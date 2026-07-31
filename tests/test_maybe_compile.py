"""Behavioral tests for the nonblocking compile trigger."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    fake_root = tmp_path / "vault"
    fake_state = tmp_path / "state"
    (fake_state / "run").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(fake_state))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(fake_root))
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    for module in ("maybe_compile", "memory_queue", "memory_state"):
        sys.modules.pop(module, None)

    import maybe_compile

    monkeypatch.setattr(maybe_compile, "ROOT", fake_root)
    monkeypatch.setattr(maybe_compile, "STATE_ROOT", fake_state)
    monkeypatch.setattr(
        maybe_compile, "LOCK_FILE", fake_state / "run" / "compile.pid"
    )
    monkeypatch.setattr(
        maybe_compile,
        "COMPILE_SCRIPT",
        fake_root / "scripts" / "compile_memory.py",
    )
    return maybe_compile


def test_unlocked_fixed_file_is_idle_regardless_of_contents(fake_env):
    fake_env.LOCK_FILE.write_text("not a pid file\n", encoding="utf-8")

    running, reason = fake_env._is_compile_running()

    assert running is False
    assert "idle" in reason
    assert fake_env.LOCK_FILE.exists()


def test_held_os_lock_is_running_then_becomes_idle_without_unlink(fake_env):
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with fake_env.compile_file_lock(fake_env.LOCK_FILE, timeout=1):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    try:
        running, reason = fake_env._is_compile_running()
        assert running is True
        assert "held" in reason
    finally:
        release.set()
        holder.join(timeout=5)

    running, reason = fake_env._is_compile_running()
    assert running is False
    assert "idle" in reason
    assert fake_env.LOCK_FILE.exists()


def test_spawn_skipped_while_os_lock_is_held(fake_env, monkeypatch):
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: True)
    spawned_calls = []
    monkeypatch.setattr(
        fake_env,
        "spawn_detached",
        lambda *args, **kwargs: spawned_calls.append(1) or 12345,
    )
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with fake_env.compile_file_lock(fake_env.LOCK_FILE, timeout=1):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    try:
        spawned, reason = fake_env.spawn_compile_if_idle()
    finally:
        release.set()
        holder.join(timeout=5)

    assert spawned is False
    assert "skipped" in reason
    assert spawned_calls == []


def test_force_refuses_held_os_lock(fake_env, monkeypatch):
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: False)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with fake_env.compile_file_lock(fake_env.LOCK_FILE, timeout=1):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    try:
        spawned, reason = fake_env.spawn_compile_if_idle(force=True)
    finally:
        release.set()
        holder.join(timeout=5)

    assert spawned is False
    assert "force refused" in reason


def test_spawn_skipped_when_no_pending_work(fake_env, monkeypatch):
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: False)
    spawned_calls = []
    monkeypatch.setattr(
        fake_env,
        "spawn_detached",
        lambda *args, **kwargs: spawned_calls.append(1) or 12345,
    )

    spawned, reason = fake_env.spawn_compile_if_idle()

    assert spawned is False
    assert "no pending" in reason
    assert spawned_calls == []


def test_spawn_happens_when_idle_and_work_pending(fake_env, monkeypatch):
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: True)
    monkeypatch.setattr(fake_env, "spawn_detached", lambda *args, **kwargs: 12345)

    spawned, reason = fake_env.spawn_compile_if_idle()

    assert spawned is True
    assert reason == "spawned compile pid=12345"
    assert fake_env.LOCK_FILE.exists()
    assert fake_env._is_compile_running()[0] is False


def test_spawn_failure_does_not_remove_fixed_lock_file(fake_env, monkeypatch):
    fake_env.LOCK_FILE.write_text("fixed\n", encoding="utf-8")
    monkeypatch.setattr(fake_env, "_has_pending_work", lambda: True)
    monkeypatch.setattr(fake_env, "spawn_detached", lambda *args, **kwargs: None)

    spawned, reason = fake_env.spawn_compile_if_idle()

    assert spawned is False
    assert reason == "spawn failed"
    assert fake_env.LOCK_FILE.read_text(encoding="utf-8") == "fixed\n"


def test_opencode_sdk_mode_ensures_one_durable_control_without_spawning(
    fake_env, monkeypatch
):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "opencode-sdk")
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-07-01.md").write_text("pending", encoding="utf-8")
    spawned_calls = []
    monkeypatch.setattr(
        fake_env,
        "spawn_detached",
        lambda *args, **kwargs: spawned_calls.append(1),
    )

    first = fake_env.spawn_compile_if_idle()
    second = fake_env.spawn_compile_if_idle()

    assert first == (False, "skipped: pending compile queued for OpenCode SDK")
    assert second == (False, "skipped: pending compile already queued for OpenCode SDK")
    assert spawned_calls == []
    [control_path] = (fake_env.STATE_ROOT / "run" / "queue").glob("*.json")
    control = json.loads(control_path.read_text(encoding="utf-8"))
    assert control["type"] == "compile"


def test_opencode_sdk_mode_does_not_create_control_without_compile_work(
    fake_env, monkeypatch
):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "opencode-sdk")

    assert fake_env.spawn_compile_if_idle(force=True) == (
        False,
        "skipped: no pending work (all daily logs compiled)",
    )
    assert not list((fake_env.STATE_ROOT / "run").glob("queue/*.json"))


def test_has_pending_work_false_when_all_compiled(fake_env):
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-01.md"
    daily.write_text("test content", encoding="utf-8")
    (fake_env.ROOT / "knowledge" / "index.md").write_text(
        "# Index\n",
        encoding="utf-8",
    )
    digest = fake_env.file_hash(daily)
    generation = "a" * 64
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "compiled_daily_hashes": {daily.name: digest},
                "compiled_daily_receipts": {
                    daily.name: {
                        "version": 1,
                        "daily_sha256": digest,
                        "generation_id": generation,
                        "journal_ids": [],
                        "effects": [],
                        "targets": [],
                        "index": {
                            "generation_id": generation,
                            "entries": [],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert fake_env._has_pending_work() is False


def test_has_pending_work_true_when_hash_differs(fake_env):
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-01.md"
    daily.write_text("new content", encoding="utf-8")
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.write_text(
        json.dumps({"compiled_daily_hashes": {daily.name: "old-hash"}}),
        encoding="utf-8",
    )

    assert fake_env._has_pending_work() is True


def test_has_pending_work_true_when_daily_not_in_state(fake_env):
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-07-01.md").write_text("x", encoding="utf-8")
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    assert fake_env._has_pending_work() is True


def test_has_pending_work_true_when_matching_hash_has_no_receipt(fake_env):
    daily_dir = fake_env.ROOT / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-01.md"
    daily.write_text("compiled bytes without receipt", encoding="utf-8")
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.write_text(
        json.dumps(
            {"compiled_daily_hashes": {daily.name: fake_env.file_hash(daily)}}
        ),
        encoding="utf-8",
    )

    assert fake_env._has_pending_work() is True


def test_has_pending_work_true_when_only_index_rebuild_is_pending(fake_env):
    state_file = fake_env.STATE_ROOT / "run" / "state.json"
    state_file.write_text(
        json.dumps({"compile_index_pending": {"batch_id": "batch-1"}}),
        encoding="utf-8",
    )

    assert fake_env._has_pending_work() is True
