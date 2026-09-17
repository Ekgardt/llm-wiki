"""The compile spawned by `maybe_compile` recognises the lock written for it.

The spawner claims a PID-0 placeholder, spawns the child and only then writes
the child's PID. A child that looked in between saw PID 0, decided the lock was
another compile's and refused itself, and the trigger silently lost that pass.
It now carries the placeholder's token. The same pass also stopped treating an
empty lock file — the window of the create-then-write fallback — as a ruin to
be removed (audit M-B6). Research:
`docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compile_memory  # noqa: E402
import maybe_compile  # noqa: E402


@pytest.fixture
def lock_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "run" / "compile.pid"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", path)
    return path


def test_the_child_accepts_the_placeholder_lock_its_spawner_wrote(lock_file) -> None:
    maybe_compile._try_claim_lock()
    token = maybe_compile.lock_owner_token()

    handle = compile_memory._acquire_compile_lock(token)

    assert handle == (compile_memory.SPAWNED_LOCK, "spawned")
    assert maybe_compile._read_lock()["pid"] == 0


def test_a_lock_with_another_token_is_not_the_childs(lock_file) -> None:
    maybe_compile._try_claim_lock()

    handle, reason = compile_memory._acquire_compile_lock("a-token-of-another-run")

    assert handle is None
    assert reason.startswith("lock held by another compile")


def test_the_spawner_keeps_the_token_when_it_records_the_child(lock_file) -> None:
    """The lock the child was told about is the lock it finds afterwards."""
    maybe_compile._try_claim_lock()
    token = maybe_compile.lock_owner_token()

    maybe_compile._write_lock(os.getpid(), token=token)

    assert maybe_compile.lock_owner_token() == token
    assert compile_memory._acquire_compile_lock(token)[1] == "spawned"


def test_the_spawn_command_names_the_lock_token(lock_file, monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(maybe_compile, "_has_pending_work", lambda: True)
    monkeypatch.setattr(
        maybe_compile,
        "spawn_detached",
        lambda command, **_kwargs: commands.append(command) or os.getpid(),
    )

    spawned, reason = maybe_compile.spawn_compile_if_idle()

    assert (spawned, "--lock-token" in commands[0]) == (True, True)
    assert commands[0][commands[0].index("--lock-token") + 1] == (
        maybe_compile.lock_owner_token()
    )
    assert reason.startswith("spawned compile")


def test_an_empty_lock_inside_the_spawn_window_is_a_write_in_progress(
    lock_file,
) -> None:
    lock_file.write_bytes(b"")

    state = maybe_compile._lock_state()
    lines = compile_memory._lock_lines(lock_file)

    assert (state[0], lines) == ("live", None)
    assert lock_file.exists()


def test_an_empty_lock_older_than_the_window_is_removed(lock_file) -> None:
    lock_file.write_bytes(b"")
    past = os.stat(lock_file).st_mtime - maybe_compile._PID0_TTL_SECONDS - 60
    os.utime(lock_file, (past, past))

    state = maybe_compile._lock_state()
    compile_memory._lock_lines(lock_file)

    assert state[0] == "stale"
    assert not lock_file.exists()
