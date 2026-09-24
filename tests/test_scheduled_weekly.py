from __future__ import annotations

import contextlib
import sqlite3
import sys
import types
from pathlib import Path

import markdown_transaction
import operational_ownership
import scheduled_weekly


def _weekly_owner_row(candidate: Path) -> tuple:
    with contextlib.closing(sqlite3.connect(candidate)) as database:
        return database.execute(
            "SELECT owner_token, fencing_epoch, expires_at FROM maintenance_owners "
            "WHERE role='weekly' AND scope='global'"
        ).fetchone()


def test_weekly_keeps_its_owner_and_marker_and_never_runs_the_nightly(
    tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    lease, marker = operational_ownership.acquire_scheduled_owner(
        "weekly", state_root=state_root
    )
    marker_path = state_root / marker.relative_path
    original_marker = marker_path.read_bytes()
    phases: list[str] = []
    outer = (
        original_marker,
        (lease.token, lease.epoch, operational_ownership._timestamp(lease.expires_at)),
    )

    def assert_outer(phase: str) -> None:
        phases.append(phase)
        assert (marker_path.read_bytes(), _weekly_owner_row(candidate)) == outer

    def nested_nightly(**_kwargs) -> int:
        raise AssertionError("the weekly must not run the nightly again")

    def run_step(_command, _log, name, **_kwargs) -> int:
        assert_outer(name)
        return 0

    monkeypatch.setattr(
        scheduled_weekly,
        "heartbeat_owner",
        lambda _ownership, **_kwargs: contextlib.nullcontext(_ownership),
    )

    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "run_nightly", nested_nightly)
    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "_run_step", run_step)
    monkeypatch.setattr(scheduled_weekly, "_wait_for_compile_idle", lambda _log: None)
    monkeypatch.setattr(scheduled_weekly, "REPORTS_DIR", tmp_path / "logs")
    monkeypatch.setattr(scheduled_weekly, "record_weekly_result", lambda *_a, **_k: None)
    monkeypatch.setitem(
        sys.modules,
        "reflection",
        types.SimpleNamespace(
            find_reflection_candidates=lambda: [], reflect_page=lambda *_a, **_k: None
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "build_tiers",
        types.SimpleNamespace(
            build_all_tiers=lambda **_kwargs: {"generated": 0, "skipped": 0}
        ),
    )

    try:
        exit_code = scheduled_weekly.run_weekly(ownership=lease)
        held = marker_path.read_bytes()
    finally:
        operational_ownership.release_marker_owner(lease, marker)

    assert (exit_code, phases[0], len(phases) >= 4, held) == (
        0,
        "okf",
        True,
        original_marker,
    )
    assert not marker_path.exists()


def test_the_weekly_pass_ages_session_records_out_of_the_active_tree():
    """The archiver only helps if the pass that runs unattended calls it."""
    steps = scheduled_weekly._script_steps()
    commands = {label: command for _message, label, command, _timeout in steps}
    labels = list(commands)
    command = commands["sessions"]

    assert labels.index("sessions") > labels.index("archive")
    assert (command[-1], Path(command[-2]).name) == ("--apply", "archive_sessions.py")
