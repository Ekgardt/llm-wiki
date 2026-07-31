from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def weekly_env(tmp_path, monkeypatch):
    import scheduled_weekly

    root = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    reports = state_root / "logs"
    (root / "scripts").mkdir(parents=True)
    (state_root / "run").mkdir(parents=True)

    monkeypatch.setattr(scheduled_weekly, "ROOT", root)
    monkeypatch.setattr(scheduled_weekly, "STATE_ROOT", state_root, raising=False)
    monkeypatch.setattr(scheduled_weekly, "REPORTS_DIR", reports)
    monkeypatch.setattr(scheduled_weekly, "_wait_for_compile_idle", lambda log: None)
    return scheduled_weekly, state_root / "run" / "maintenance.lock", reports


def test_weekly_holds_one_lease_and_rebuilds_only_after_final_page_mutation(
    weekly_env, monkeypatch
):
    scheduled_weekly, lock_path, _reports = weekly_env
    events: list[tuple[str, bool]] = []

    def nightly_main(*, acquire_lease=True, rebuild_indexes=True):
        events.append((f"nightly:{acquire_lease}:{rebuild_indexes}", lock_path.exists()))
        return 0

    def run_step(_cmd, _log, label, timeout):
        events.append((label, lock_path.exists()))
        return 0

    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "main", nightly_main)
    monkeypatch.setattr(scheduled_weekly, "_run_step", run_step)
    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "_run_step", run_step)
    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "ROOT", scheduled_weekly.ROOT)

    rc = scheduled_weekly.main()

    assert rc == 0
    assert events == [
        ("nightly:False:False", True),
        ("okf", True),
        ("archive", True),
        ("markdown-index", True),
        ("search", True),
        ("graph", True),
    ]
    assert lock_path.exists()
    with scheduled_weekly.advisory_file_lock(lock_path, timeout=0):
        pass


def test_weekly_contention_is_logged_and_returns_nonzero(weekly_env):
    import memory_state

    scheduled_weekly, lock_path, reports = weekly_env

    with memory_state.advisory_file_lock(lock_path, timeout=0):
        rc = scheduled_weekly.main()

    assert rc == 1
    report = next(reports.glob("weekly-*.md")).read_text(encoding="utf-8")
    assert "maintenance lock contended" in report
    assert str(lock_path) in report


def test_weekly_final_rebuild_failure_is_truthful_and_nonzero(weekly_env, monkeypatch):
    scheduled_weekly, _lock_path, reports = weekly_env
    monkeypatch.setattr(
        scheduled_weekly,
        "scheduled_nightly",
        SimpleNamespace(main=lambda **_kwargs: 0),
        raising=False,
    )
    monkeypatch.setattr(scheduled_weekly, "_run_step", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(scheduled_weekly, "_rebuild_derived_indexes", lambda log: 1, raising=False)

    rc = scheduled_weekly.main()

    assert rc == 1
    report = next(reports.glob("weekly-*.md")).read_text(encoding="utf-8")
    assert "final derived-index rebuild failed" in report
    assert "failures=1" in report


def test_weekly_reports_nightly_exception_and_releases_lease(weekly_env, monkeypatch):
    scheduled_weekly, lock_path, reports = weekly_env

    def fail(**_kwargs):
        assert lock_path.exists()
        raise RuntimeError("nightly crashed")

    monkeypatch.setattr(
        scheduled_weekly,
        "scheduled_nightly",
        SimpleNamespace(main=fail),
        raising=False,
    )

    monkeypatch.setattr(scheduled_weekly, "_run_step", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(scheduled_weekly, "_rebuild_derived_indexes", lambda _log: 0)

    rc = scheduled_weekly.main()

    assert rc == 1
    assert lock_path.exists()
    with scheduled_weekly.advisory_file_lock(lock_path, timeout=0):
        pass
    report = next(reports.glob("weekly-*.md")).read_text(encoding="utf-8")
    assert "nightly maintenance raised RuntimeError: nightly crashed" in report


def test_weekly_preserves_and_reports_exhausted_queue_tasks(weekly_env, monkeypatch):
    import memory_queue

    scheduled_weekly, _lock_path, reports = weekly_env
    task_id = "20260726-120000-deadbeef"
    queue_dir = scheduled_weekly.STATE_ROOT / "run" / "queue"
    queue_dir.mkdir()
    task_path = queue_dir / f"{task_id}.json"
    task_path.write_text(
        json.dumps(
            {
                "id": task_id,
                "type": "query",
                "enqueued_at": "2026-07-26T12:00:00",
                "enqueue_sequence": 1,
                "attempts": 5,
                "payload": {"prompt": "SECRET_PROMPT_CONTENT"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    before = task_path.read_bytes()
    commands: list[list[str]] = []

    monkeypatch.setattr(
        scheduled_weekly,
        "scheduled_nightly",
        SimpleNamespace(main=lambda **_kwargs: 0),
        raising=False,
    )
    monkeypatch.setattr(memory_queue, "_queue_dir", lambda: queue_dir)

    def run_step(cmd, *_args, **_kwargs):
        commands.append(cmd)
        if "clear-failed" in cmd:
            task_path.unlink()
        return 0

    monkeypatch.setattr(scheduled_weekly, "_run_step", run_step)
    monkeypatch.setattr(scheduled_weekly, "_rebuild_derived_indexes", lambda _log: 0)

    rc = scheduled_weekly.main()

    assert rc == 1
    assert task_path.read_bytes() == before
    assert all("clear-failed" not in cmd for cmd in commands)
    report = next(reports.glob("weekly-*.md")).read_text(encoding="utf-8")
    assert "1 exhausted queue task(s) require human review" in report
    assert task_id in report
    assert "SECRET_PROMPT_CONTENT" not in report
