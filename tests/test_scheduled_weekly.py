from __future__ import annotations

import contextlib
import sqlite3
import sys
import types
from pathlib import Path

import markdown_transaction
import operational_ownership
import scheduled_weekly


def test_weekly_keeps_outer_owner_and_marker_while_running_nested_nightly_work(
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

    def assert_outer(phase: str) -> None:
        phases.append(phase)
        assert marker_path.read_bytes() == original_marker
        with sqlite3.connect(candidate) as database:
            assert database.execute(
                "SELECT owner_token, fencing_epoch, expires_at FROM maintenance_owners "
                "WHERE role='weekly' AND scope='global'"
            ).fetchone() == (
                lease.token,
                lease.epoch,
                lease.expires_at.isoformat().replace("+00:00", "Z"),
            )

    def nested_nightly(*, ownership) -> int:
        assert ownership == lease
        assert_outer("nightly")
        return 0

    def run_step(_command, _log, name, **_kwargs) -> int:
        assert _kwargs["ownership"] == lease
        assert_outer(name)
        return 0

    monkeypatch.setattr(
        scheduled_weekly,
        "heartbeat_owner",
        lambda _ownership: contextlib.nullcontext(_ownership),
    )

    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "run_nightly", nested_nightly)
    monkeypatch.setattr(scheduled_weekly, "_run_step", run_step)
    monkeypatch.setattr(scheduled_weekly, "_wait_for_compile_idle", lambda _log: None)
    monkeypatch.setattr(scheduled_weekly, "REPORTS_DIR", tmp_path / "logs")
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
        assert scheduled_weekly.run_weekly(ownership=lease) == 0
        assert phases[0] == "nightly"
        assert len(phases) >= 4
        assert marker_path.read_bytes() == original_marker
    finally:
        operational_ownership.release_marker_owner(lease, marker)

    assert not marker_path.exists()


def test_the_weekly_pass_ages_session_records_out_of_the_active_tree():
    """The archiver only helps if the pass that runs unattended calls it."""
    import scheduled_weekly

    steps = scheduled_weekly._script_steps()
    labels = [label for _message, label, _command, _timeout in steps]
    sessions = next(step for step in steps if step[1] == "sessions")

    assert labels.index("sessions") > labels.index("archive")
    assert sessions[2][-1] == "--apply"
    assert sessions[2][-2].endswith("archive_sessions.py")
