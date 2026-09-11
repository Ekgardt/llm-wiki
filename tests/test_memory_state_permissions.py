from __future__ import annotations

import os
import time

import pytest


def test_update_state_honors_lock_timeout(tmp_path, monkeypatch):
    import memory_state

    state_dir = tmp_path / "run"
    lock_file = state_dir / "state.json.lock"
    state_dir.mkdir()
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", lock_file)

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        memory_state.update_state(lambda state: state.update(value=1), lock_timeout=0.1)

    assert time.perf_counter() - started < 0.75


def _permission_error(winerror: int | None) -> PermissionError:
    error = PermissionError("denied")
    error.winerror = winerror
    return error


def test_state_lock_retries_windows_sharing_violation(tmp_path, monkeypatch):
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(memory_state.sys, "platform", "win32")
    real_open = os.open
    attempts = 0

    def sharing_once(path, flags):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _permission_error(32)
        return real_open(path, flags)

    monkeypatch.setattr(memory_state.os, "open", sharing_once)
    with memory_state._state_lock(timeout=0.2, poll=0):
        pass
    assert attempts == 2


def test_state_lock_retries_windows_eacces_without_winerror(tmp_path, monkeypatch):
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(memory_state.sys, "platform", "win32")
    real_open = os.open
    attempts = 0

    def eacces_once(path, flags):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags)

    monkeypatch.setattr(memory_state.os, "open", eacces_once)
    with memory_state._state_lock(timeout=0.2, poll=0):
        pass
    assert attempts == 2


def test_state_lock_retries_permission_race_after_observed_contention(
    tmp_path, monkeypatch
):
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    real_open = os.open
    attempts = 0

    def transient_sequence(path, flags):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileExistsError(path)
        if attempts == 2:
            raise _permission_error(5)
        return real_open(path, flags)

    monkeypatch.setattr(memory_state.os, "open", transient_sequence)
    with memory_state._state_lock(timeout=0.2, poll=0):
        pass
    assert attempts == 3


def test_state_lock_fails_fast_for_acl_permission_error(tmp_path, monkeypatch):
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(
        memory_state.os, "open", lambda *args: (_ for _ in ()).throw(_permission_error(5))
    )

    with pytest.raises(PermissionError):
        with memory_state._state_lock(timeout=0.2, poll=0):
            pass


def test_atomic_write_uses_unique_recoverable_durable_staging(tmp_path, monkeypatch):
    import memory_state
    from reliable_memory import MetadataDurabilityUnavailable

    target = tmp_path / "run" / "state.json"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    observed = []

    def fail(staged, destination, **options):
        observed.append((staged, destination, options))
        raise MetadataDurabilityUnavailable("move failed")

    monkeypatch.setattr(memory_state, "durable_publish_file", fail)
    with pytest.raises(MetadataDurabilityUnavailable):
        memory_state.atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert len(observed) == 1
    staged, destination, options = observed[0]
    assert destination == target
    assert staged.parent == target.parent
    assert staged.name.startswith(".state.json.")
    assert staged.read_bytes() == b"new"
    assert options["replace"] is True


def test_atomic_write_replaces_longer_existing_file(tmp_path):
    import memory_state

    target = tmp_path / "run" / "state.json"
    target.parent.mkdir()
    target.write_text("existing payload is longer", encoding="utf-8")

    memory_state.atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


def test_save_state_routes_through_atomic_write(tmp_path, monkeypatch):
    import memory_state

    state_dir = tmp_path / "run"
    state_file = state_dir / "state.json"
    calls = []
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(
        memory_state,
        "atomic_write",
        lambda path, content, encoding="utf-8": calls.append((path, content, encoding)),
    )

    memory_state.save_state({"value": 1})

    assert calls == [(state_file, '{\n  "value": 1\n}', "utf-8")]


def test_a_stale_lock_is_retired_only_while_it_holds_the_judged_bytes(tmp_path):
    """Audit OPS-07: a fresh owner's lock moved aside by mistake is put back."""
    import memory_state

    lock = tmp_path / "state.json.lock"
    lock.write_bytes(b"4242")

    assert memory_state.retire_stale_lock(lock, b"4242") is True
    assert not lock.exists()
    assert list(tmp_path.iterdir()) == []

    lock.write_bytes(b"9999")
    assert memory_state.retire_stale_lock(lock, b"4242") is False
    assert lock.read_bytes() == b"9999"
    assert [path.name for path in tmp_path.iterdir()] == ["state.json.lock"]

    assert memory_state.retire_stale_lock(tmp_path / "absent.lock", b"1") is False


def test_a_dead_state_lock_is_retired_and_a_live_one_waited_out(tmp_path, monkeypatch):
    import memory_state

    lock = tmp_path / "state.json.lock"
    monkeypatch.setattr(memory_state, "LOCK_FILE", lock)
    monkeypatch.setattr(memory_state, "_lock_age", lambda: memory_state._STALE_LOCK_SECONDS + 1)
    lock.write_bytes(b"777")
    monkeypatch.setattr(memory_state, "_is_pid_alive", lambda pid: False)

    memory_state._await_lock_turn(deadline=time.time() + 5, poll=0.01)

    assert not lock.exists()
