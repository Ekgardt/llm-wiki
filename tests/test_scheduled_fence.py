"""The nightly and weekly passes take the canonical fence (decision 2026-09-10).

A dead owner's marker is reclaimed only with the registry's proof; a live
owner refuses by name; a lost fence stops the pass between steps and is
recorded instead of success; a vault without a V3 coordinator keeps the
legacy marker. See docs/research/2026-09-10-the-nightly-and-the-fence-it-never-takes.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import markdown_transaction  # noqa: E402
import operational_ownership as ownership  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

MARKER = "run/maintenance.lock"


def _candidate(state_root: Path) -> Path:
    path = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return path


def _expire_owner(candidate: Path, lease: ownership.OwnerLease) -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with contextlib.closing(sqlite3.connect(candidate)) as database:
        database.execute(
            "UPDATE maintenance_owners SET expires_at=? WHERE owner_token=?",
            (past, lease.token),
        )
        database.commit()


def _owner_rows(candidate: Path) -> list[tuple[str, int]]:
    with contextlib.closing(sqlite3.connect(candidate)) as database:
        return database.execute(
            "SELECT role, process_id FROM maintenance_owners ORDER BY role"
        ).fetchall()


# --- the registry reclaims a dead owner's marker -----------------------------


def test_a_dead_owners_marker_is_reclaimed_and_the_fence_taken(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    candidate = _candidate(state_root)
    lease, _marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)
    _expire_owner(candidate, lease)
    # The owner "died": its PID is ours, so the probe must be told it is dead.
    monkeypatch.setattr(
        ownership.OwnershipRegistry, "_probed_state", lambda _self, _identity: "dead"
    )

    second, marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)

    try:
        assert second.epoch == lease.epoch + 1
        assert _owner_rows(candidate) == [("nightly", os.getpid())]
        assert (state_root / MARKER).read_bytes() == str(os.getpid()).encode("ascii")
    finally:
        ownership.release_marker_owner(second, marker)
    assert not (state_root / MARKER).exists()


def test_a_live_owners_marker_refuses_by_name(tmp_path):
    state_root = tmp_path / "state"
    _candidate(state_root)
    lease, marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)

    try:
        with pytest.raises(ownership.OperationalOwnershipError) as error:
            ownership.acquire_scheduled_owner("nightly", state_root=state_root)
        assert error.value.code == "owner_busy"
        assert (state_root / MARKER).read_bytes() == str(os.getpid()).encode("ascii")
    finally:
        ownership.release_marker_owner(lease, marker)


def test_an_expired_owner_whose_process_is_alive_is_not_stolen(tmp_path):
    """Age alone is never proof: the process is ours and alive."""
    state_root = tmp_path / "state"
    candidate = _candidate(state_root)
    lease, marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)
    _expire_owner(candidate, lease)

    try:
        with pytest.raises(ownership.OperationalOwnershipError) as error:
            ownership.acquire_scheduled_owner("nightly", state_root=state_root)
        assert error.value.code == "owner_busy"
    finally:
        ownership.release_marker_owner(lease, marker)


def test_an_ownerless_marker_of_a_dead_pid_is_removed(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    _candidate(state_root)
    marker_path = state_root / MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_bytes(b"4000000")
    real_probe = ownership.process_start_identity
    monkeypatch.setattr(
        ownership,
        "process_start_identity",
        lambda pid: None if pid == 4000000 else real_probe(pid),
    )

    lease, marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)

    try:
        assert marker_path.read_bytes() == str(os.getpid()).encode("ascii")
    finally:
        ownership.release_marker_owner(lease, marker)


def test_an_ownerless_marker_of_a_living_pid_refuses(tmp_path):
    state_root = tmp_path / "state"
    _candidate(state_root)
    marker_path = state_root / MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_bytes(str(os.getpid()).encode("ascii"))

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        ownership.acquire_scheduled_owner("nightly", state_root=state_root)

    assert error.value.code == "owner_busy"
    assert marker_path.read_bytes() == str(os.getpid()).encode("ascii")


def test_an_unreadable_ownerless_marker_refuses_by_name(tmp_path):
    state_root = tmp_path / "state"
    _candidate(state_root)
    marker_path = state_root / MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_bytes(b"not a pid")

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        ownership.acquire_scheduled_owner("nightly", state_root=state_root)

    assert error.value.code == "marker_identity_invalid"


# --- a lost fence is visible to the body ------------------------------------


def test_the_heartbeat_sets_the_lost_event_when_the_fence_is_gone(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    _candidate(state_root)
    lease, marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)
    lost = threading.Event()
    beats = iter((False, True))
    monkeypatch.setattr(
        ownership, "_wait_for_owner_heartbeat", lambda _stop, _seconds: next(beats, True)
    )
    monkeypatch.setattr(
        ownership.OwnershipRegistry,
        "heartbeat",
        lambda _self, _lease: (_ for _ in ()).throw(
            ownership.OperationalOwnershipError("owner_fence_lost")
        ),
    )

    with pytest.raises(ownership.OperationalOwnershipError):
        with ownership.heartbeat_owner(lease, lost=lost):
            assert lost.wait(SHORT_TIMEOUT)
    ownership.release_marker_owner(lease, marker)


def _state(tmp_path: Path, monkeypatch) -> Path:
    import memory_state
    import scheduled_nightly

    state_dir = tmp_path / "state" / "run"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(scheduled_nightly, "update_state", memory_state.update_state)
    monkeypatch.setattr(scheduled_nightly, "REPORTS_DIR", tmp_path / "logs")
    return state_dir / "state.json"


def test_a_fence_lost_between_steps_stops_the_pass_and_is_recorded(tmp_path, monkeypatch):
    import scheduled_nightly

    state_file = _state(tmp_path, monkeypatch)
    fence = threading.Event()
    ran: list[str] = []

    def run_step(_command, _log, name, *, timeout):
        ran.append(name)
        fence.set()
        return 0

    monkeypatch.setattr(scheduled_nightly, "_run_step", run_step)

    with pytest.raises(ownership.OperationalOwnershipError, match="owner_fence_lost"):
        scheduled_nightly._run_nightly_body(ownership=None, fence=fence)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert ran == ["capture_adoption"]
    assert state["last_nightly_status"] == "failed"
    assert "owner_fence_lost" in state["last_nightly_failure"]["error"]


def test_a_fence_lost_during_the_last_step_is_recorded_not_success(tmp_path, monkeypatch):
    import scheduled_nightly

    state_file = _state(tmp_path, monkeypatch)
    fence = threading.Event()
    monkeypatch.setattr(scheduled_nightly, "_nightly_steps", lambda *_a, **_k: 0)
    monkeypatch.setattr(scheduled_nightly, "_prune_reports", lambda _log: None)
    monkeypatch.setattr(scheduled_nightly, "_update_code", lambda _log: fence.set())

    assert scheduled_nightly._run_nightly_body(ownership=None, fence=fence) == 0

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_nightly_status"] == "failed"
    assert "owner_fence_lost" in state["last_nightly_failure"]["error"]


# --- main takes the canonical fence on an adopted vault ---------------------


def test_main_holds_a_nightly_row_while_the_pass_runs(tmp_path, monkeypatch):
    import scheduled_nightly

    state_root = tmp_path / "state"
    _state(tmp_path, monkeypatch)
    candidate = _candidate(state_root)
    registry = ownership.OwnershipRegistry(state_root)
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", state_root)
    monkeypatch.setattr(scheduled_nightly, "adopted_ownership_registry", lambda _r, _s: registry)
    seen: list[list[tuple[str, int]]] = []

    def body(*, ownership, fence=None):
        seen.append(_owner_rows(candidate))
        return 0

    monkeypatch.setattr(scheduled_nightly, "_run_nightly_body", body)

    assert scheduled_nightly.main() == 0

    assert seen == [[("nightly", os.getpid())]]
    assert _owner_rows(candidate) == []
    assert not (state_root / MARKER).exists()


def test_main_skips_by_the_registrys_reason_when_the_fence_is_held(tmp_path, monkeypatch):
    import scheduled_nightly

    state_root = tmp_path / "state"
    state_file = _state(tmp_path, monkeypatch)
    _candidate(state_root)
    registry = ownership.OwnershipRegistry(state_root)
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", state_root)
    monkeypatch.setattr(scheduled_nightly, "adopted_ownership_registry", lambda _r, _s: registry)
    lease, marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)

    try:
        assert scheduled_nightly.main() == 0
    finally:
        ownership.release_marker_owner(lease, marker)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_nightly_skip"]["reason"] == "owner_busy"


def test_a_vault_without_a_coordinator_keeps_the_legacy_marker(tmp_path, monkeypatch):
    import scheduled_nightly

    state_root = tmp_path / "state"
    _state(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", state_root)
    monkeypatch.setattr(scheduled_nightly, "adopted_ownership_registry", lambda _r, _s: None)
    seen: list[bytes] = []

    def body(*, ownership, fence=None):
        assert ownership is None
        seen.append((state_root / MARKER).read_bytes())
        return 0

    monkeypatch.setattr(scheduled_nightly, "_run_nightly_body", body)

    assert scheduled_nightly.main() == 0

    assert seen == [str(os.getpid()).encode("ascii")]
    assert not (state_root / MARKER).exists()
