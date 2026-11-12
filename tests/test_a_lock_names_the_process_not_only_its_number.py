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

import compile_lock  # noqa: E402
import memory_state  # noqa: E402
import process_liveness  # noqa: E402


def _reused_identity() -> str:
    identity = process_liveness.process_start_identity(os.getpid())
    prefix, ticks = identity.rsplit(":", 1)
    return f"{prefix}:{int(ticks) + 1}"


FOREIGN = _reused_identity()


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    return directory


def _mark(state: dict) -> None:
    state["written"] = True


def test_the_state_lock_of_a_reused_pid_is_retired_without_waiting_out_its_age(
    state_dir,
) -> None:
    """Our own PID, somebody else's process: the lock is not ours to wait for."""
    memory_state.lock_file().write_bytes(f"{os.getpid()}\n{FOREIGN}\n".encode())
    started = time.monotonic()

    memory_state.update_state(_mark)

    assert time.monotonic() - started < memory_state.STALE_LOCK_SECONDS
    assert not memory_state.lock_file().exists()


def test_a_live_owner_keeps_the_state_lock_it_holds(state_dir) -> None:
    identity = process_liveness.process_start_identity(os.getpid())
    memory_state.lock_file().write_bytes(f"{os.getpid()}\n{identity}\n".encode())

    with pytest.raises(memory_state.StateLockTimeout):
        memory_state.update_state(_mark, lock_timeout=0.2)

    assert memory_state.lock_file().exists()


def test_a_lock_written_before_this_release_is_still_judged_by_its_age(
    state_dir,
) -> None:
    """No identity line: the PID probe and the 30 s rule, exactly as before."""
    memory_state.lock_file().write_bytes(str(os.getpid()).encode())

    with pytest.raises(memory_state.StateLockTimeout):
        memory_state.update_state(_mark, lock_timeout=0.2)

    assert memory_state.lock_owner(memory_state.lock_file().read_bytes()) == (
        os.getpid(),
        "",
    )


def test_the_state_lock_records_this_process(state_dir) -> None:
    payload = memory_state.lock_payload()

    assert memory_state.lock_owner(payload) == (
        os.getpid(),
        process_liveness.process_start_identity(os.getpid()),
    )
    assert memory_state.named_owner_is_dead(payload) is False


def test_a_compile_lock_of_a_reused_pid_is_stale(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    lock = compile_lock.compile_lock_file()
    lock.parent.mkdir()
    stamp = "2026-09-17T12:00:00"
    lock.write_bytes(f"{os.getpid()}\n{stamp}\ntoken\n{FOREIGN}\n".encode())

    assert compile_lock.lock_state()[0] == "stale"


def test_a_compile_lock_this_process_holds_is_live(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    lock = compile_lock.compile_lock_file()
    lock.parent.mkdir()
    compile_lock.write_lock(os.getpid())

    assert compile_lock.lock_state()[0] == "live"
    assert compile_lock.read_lock()["identity"] == (
        process_liveness.process_start_identity(os.getpid())
    )
