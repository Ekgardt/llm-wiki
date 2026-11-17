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
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compile_lock  # noqa: E402
import maybe_compile  # noqa: E402
from llm_wiki import compile_pipeline_selection  # noqa: E402


@pytest.fixture
def lock_file(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    path = compile_lock.compile_lock_file()
    path.parent.mkdir(parents=True)
    return path


def test_the_child_accepts_the_placeholder_lock_its_spawner_wrote(lock_file) -> None:
    compile_lock.try_claim_lock()
    token = compile_lock.lock_owner_token()

    handle = compile_pipeline_selection.acquire_compile_lock(token)

    assert handle == (compile_pipeline_selection.SPAWNED_LOCK, "spawned")
    assert compile_lock.read_lock()["pid"] == 0


def test_a_lock_with_another_token_is_not_the_childs(lock_file) -> None:
    compile_lock.try_claim_lock()

    handle, reason = compile_pipeline_selection.acquire_compile_lock("a-token-of-another-run")

    assert handle is None
    assert reason.startswith("lock held by another compile")


def test_the_spawner_keeps_the_token_when_it_records_the_child(lock_file) -> None:
    """The lock the child was told about is the lock it finds afterwards."""
    compile_lock.try_claim_lock()
    token = compile_lock.lock_owner_token()

    compile_lock.write_lock(os.getpid(), token=token)

    assert compile_lock.lock_owner_token() == token
    assert compile_pipeline_selection.acquire_compile_lock(token)[1] == "spawned"


def test_the_spawn_command_names_the_lock_token(lock_file, monkeypatch) -> None:
    commands: list[list[str]] = []
    deadlines: list[float] = []

    def pending(_closed_days_only, *, deadline):
        deadlines.append(deadline)
        return True

    monkeypatch.setattr(maybe_compile, "has_pending_work", pending)
    monkeypatch.setattr(
        maybe_compile,
        "spawn_detached",
        lambda command, **_kwargs: commands.append(command) or os.getpid(),
    )

    deadline = time.monotonic() + 10
    spawned, reason = maybe_compile.spawn_compile_if_idle(deadline=deadline)

    assert (spawned, "--lock-token" in commands[0]) == (True, True)
    assert commands[0][commands[0].index("--lock-token") + 1] == (
        compile_lock.lock_owner_token()
    )
    assert reason.startswith("spawned compile")
    assert deadlines == [deadline]


def test_an_empty_lock_inside_the_spawn_window_is_a_write_in_progress(
    lock_file,
) -> None:
    lock_file.write_bytes(b"")

    state = compile_lock.lock_state()
    lines = compile_pipeline_selection.lock_lines(lock_file)

    assert (state[0], lines) == ("live", None)
    assert lock_file.exists()


def test_an_empty_lock_older_than_the_window_is_removed(lock_file) -> None:
    lock_file.write_bytes(b"")
    past = os.stat(lock_file).st_mtime - compile_lock.PID0_TTL_SECONDS - 60
    os.utime(lock_file, (past, past))

    state = compile_lock.lock_state()
    compile_pipeline_selection.lock_lines(lock_file)

    assert state[0] == "stale"
    assert not lock_file.exists()


def test_direct_compile_recovers_a_reaped_process_lock(lock_file) -> None:
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
    ) as child:
        compile_lock.write_lock(child.pid)
        child.communicate(timeout=10)

    handle, reason = compile_pipeline_selection.acquire_compile_lock()

    assert reason == "claimed"
    assert handle == compile_lock.lock_owner_token()
    assert compile_lock.read_lock()["pid"] == os.getpid()
    compile_pipeline_selection.release_compile_lock(handle)
    assert not lock_file.exists()


@pytest.mark.parametrize("content", [b"", b" \n"])
def test_cleanup_preserves_a_lock_still_being_written(lock_file, content) -> None:
    lock_file.write_bytes(content)

    assert compile_lock.clear_lock() is False
    assert lock_file.read_bytes() == content
    assert compile_pipeline_selection.acquire_compile_lock()[0] is None
    assert lock_file.read_bytes() == content


@pytest.mark.parametrize("state", ["alive", "unknown"])
def test_direct_compile_preserves_another_possible_owner(lock_file, monkeypatch, state):
    lock_file.write_text("123456\n2026-01-01T00:00:00\nother-owner\n\n")
    original = lock_file.read_bytes()
    monkeypatch.setattr(maybe_compile.process_liveness, "process_state", lambda _pid: state)

    assert compile_pipeline_selection.acquire_compile_lock()[0] is None
    assert lock_file.read_bytes() == original


def test_direct_compile_recovers_an_expired_empty_placeholder(lock_file) -> None:
    lock_file.write_bytes(b"")
    past = lock_file.stat().st_mtime - compile_lock.PID0_TTL_SECONDS - 60
    os.utime(lock_file, (past, past))

    handle, reason = compile_pipeline_selection.acquire_compile_lock()

    assert reason == "claimed"
    compile_pipeline_selection.release_compile_lock(handle)
    assert not lock_file.exists()
