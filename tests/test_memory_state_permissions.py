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
