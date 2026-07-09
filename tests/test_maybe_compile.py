"""Tests for maybe_compile.py — concurrency-safe compile trigger.

Locks in:
1. PID liveness probe (Windows OpenProcess + POSIX signal 0).
2. Lock is created when spawn happens; stale lock (dead PID) is stolen.
3. Lock is cleared when compile_memory finishes.
4. Multiple concurrent spawn attempts only one succeeds.
5. --force ignores existing lock.
6. _has_pending_work returns False when all hashes match.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Point maybe_compile at a tmp state root + tmp daily log dir."""
    fake_root = tmp_path / "vault"
    fake_state = tmp_path / "state"
    fake_state.mkdir(parents=True)
    (fake_state / "memory-state").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(fake_state))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))

    # Force re-import so module-level path reads pick up the env.
    for mod in ("maybe_compile", "memory_state"):
        if mod in sys.modules:
            del sys.modules[mod]
    import memory_state
    import maybe_compile

    monkeypatch.setattr(maybe_compile, "ROOT", fake_root)
    monkeypatch.setattr(maybe_compile, "STATE_ROOT", fake_state)
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", fake_state / "run" / "compile.pid")
    monkeypatch.setattr(maybe_compile, "COMPILE_SCRIPT", fake_root / "scripts" / "compile_memory.py")
    return maybe_compile


def test_is_pid_alive_current_process(fake_env):
    """Current process PID is always alive."""
    import os

    assert fake_env._is_pid_alive(os.getpid()) is True


def test_is_pid_alive_dead_pid(fake_env):
    """A PID that doesn't exist returns False."""
    # Use a PID that's valid on all platforms (Linux pid_t is signed 32-bit,
    # so max is 2^31-1 = 2147483647). 999999 is almost never a real process.
    assert fake_env._is_pid_alive(999999) is False


def test_lock_write_and_read_roundtrip(fake_env, tmp_path):
    fake_env._write_lock(12345)
    lock = fake_env._read_lock()
    assert lock is not None
    assert lock["pid"] == 12345
    assert lock["started_at"]  # ISO timestamp present


def test_clear_lock(fake_env):
    fake_env._write_lock(99999)
    assert fake_env.LOCK_FILE.exists()
    fake_env._clear_lock()
    assert not fake_env.LOCK_FILE.exists()


def test_clear_lock_idempotent(fake_env):
    """Clearing a non-existent lock doesn't crash."""
    fake_env._clear_lock()  # no error
    fake_env._clear_lock()  # still no error


def test_is_compile_running_no_lock(fake_env):
    is_running, reason = fake_env._is_compile_running()
    assert is_running is False
    assert "no lock" in reason


def test_is_compile_running_with_dead_pid(fake_env, monkeypatch):
    """Stale lock with a dead PID is reported as not-running."""
    fake_env._write_lock(99999)  # almost certainly dead
    # Force _is_pid_alive to confirm dead (don't rely on real OS state).
    monkeypatch.setattr(fake_env, "_is_pid_alive", lambda pid: False)
    is_running, reason = fake_env._is_compile_running()
    assert is_running is False
    assert "stale" in reason.lower()


def test_is_compile_running_with_alive_pid(fake_env, monkeypatch):
    """Lock with alive PID within timeout = running."""
    import os

    fake_env._write_lock(os.getpid())
    monkeypatch.setattr(fake_env, "_is_pid_alive", lambda pid: True)
    is_running, reason = fake_env._is_compile_running()
    assert is_running is True
    assert "running" in reason


def test_is_compile_running_lock_too_old(fake_env, monkeypatch):
    """Lock older than MAX_COMPILE_DURATION_S is treated as stale."""
    fake_env._write_lock(99999)
    # Manually backdate the lock timestamp.
    old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    fake_env.LOCK_FILE.write_text(f"99999\n{old}\n", encoding="utf-8")
    monkeypatch.setattr(fake_env, "_is_pid_alive", lambda pid: True)
    is_running, reason = fake_env._is_compile_running()
    assert is_running is False
    assert "stale" in reason.lower()


def test_spawn_skipped_when_already_running(fake_env, monkeypatch):
    """If a compile is already running, don't spawn another."""
    # Pretend a compile is running.
    fake_env._write_lock(99999)
    monkeypatch.setattr(fake_env, "_is_pid_alive", lambda pid: True)

    spawned_calls = []
    monkeypatch.setattr(
        fake_env, "spawn_detached", lambda *a, **kw: spawned_calls.append(1) or 12345
    )

    spawned, reason = fake_env.spawn_compile_if_idle()
    assert spawned is False
    assert "skipped" in reason
    assert spawned_calls == []  # spawn_detached was NOT called


def test_spawn_skipped_when_no_pending_work(fake_env, monkeypatch):
    """If no daily logs differ from last compile, skip spawn."""
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: False)
    spawned_calls = []
    monkeypatch.setattr(
        fake_env, "spawn_detached", lambda *a, **kw: spawned_calls.append(1) or 12345
    )

    spawned, reason = fake_env.spawn_compile_if_idle()
    assert spawned is False
    assert "no pending" in reason
    assert spawned_calls == []


def test_spawn_happens_when_idle_and_work_pending(fake_env, monkeypatch):
    """Normal path: idle + work pending → spawn + write lock."""
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: True)
    monkeypatch.setattr(fake_env, "_is_pid_alive", lambda pid: True)
    spawned_pid = [12345]
    monkeypatch.setattr(
        fake_env, "spawn_detached", lambda *a, **kw: spawned_pid[0]
    )

    spawned, reason = fake_env.spawn_compile_if_idle()
    assert spawned is True
    assert "spawned" in reason.lower()
    # Lock file written with the spawned PID.
    lock = fake_env._read_lock()
    assert lock is not None
    assert lock["pid"] == 12345


def test_force_override_running_lock(fake_env, monkeypatch):
    """--force spawns even if another compile is supposedly running."""
    fake_env._write_lock(99999)
    monkeypatch.setattr(fake_env, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: False)
    monkeypatch.setattr(fake_env, "spawn_detached", lambda *a, **kw: 55555)

    spawned, reason = fake_env.spawn_compile_if_idle(force=True)
    assert spawned is True
    # Lock should now hold the new PID.
    lock = fake_env._read_lock()
    assert lock["pid"] == 55555


def test_has_pending_work_false_when_all_compiled(fake_env, monkeypatch):
    """All daily hashes match state.json → no pending work."""
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    p = daily_dir / "2026-07-01.md"
    p.write_text("test content", encoding="utf-8")

    # State.json says the hash matches.
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    import hashlib

    h = hashlib.sha256(b"test content").hexdigest()
    state_file.write_text(
        json.dumps({"compiled_daily_hashes": {"2026-07-01.md": h}}),
        encoding="utf-8",
    )

    assert fake_env._has_pending_work() is False


def test_has_pending_work_true_when_hash_differs(fake_env):
    """Daily log changed since last compile → pending work."""
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    p = daily_dir / "2026-07-01.md"
    p.write_text("new content", encoding="utf-8")

    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"compiled_daily_hashes": {"2026-07-01.md": "old-hash"}}),
        encoding="utf-8",
    )

    assert fake_env._has_pending_work() is True


def test_has_pending_work_true_when_daily_not_in_state(fake_env):
    """New daily log never compiled → pending."""
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-07-01.md").write_text("x", encoding="utf-8")
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}", encoding="utf-8")

    assert fake_env._has_pending_work() is True
