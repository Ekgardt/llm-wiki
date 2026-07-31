from __future__ import annotations

import errno
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def nightly_env(tmp_path, monkeypatch):
    import scheduled_nightly

    root = tmp_path / "vault"
    state_root = tmp_path / "runtime"
    reports = state_root / "logs"
    (root / "scripts").mkdir(parents=True)
    (state_root / "run").mkdir(parents=True)

    monkeypatch.setattr(scheduled_nightly, "ROOT", root)
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", state_root)
    monkeypatch.setattr(scheduled_nightly, "REPORTS_DIR", reports)
    monkeypatch.setattr(scheduled_nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(scheduled_nightly.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    return scheduled_nightly, reports


def _report_text(reports: Path) -> str:
    return next(reports.glob("nightly-*.md")).read_text(encoding="utf-8")


def test_maintenance_lock_is_released_when_holder_process_terminates(tmp_path):
    import memory_state

    lock_path = tmp_path / "maintenance.lock"
    script = (
        "import sys, time; from pathlib import Path; "
        "from memory_state import advisory_file_lock; "
        "lock = advisory_file_lock(Path(sys.argv[1]), timeout=1); "
        "lock.__enter__(); print('locked', flush=True); time.sleep(60)"
    )
    env = os.environ.copy()
    scripts = str(Path(memory_state.__file__).resolve().parent)
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        process.terminate()
        process.wait(timeout=5)

        with memory_state.advisory_file_lock(lock_path, timeout=1):
            pass
        assert lock_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_nightly_contention_is_logged_and_returns_nonzero(nightly_env):
    import memory_state

    scheduled_nightly, reports = nightly_env
    lock_path = scheduled_nightly.STATE_ROOT / "run" / "maintenance.lock"

    with memory_state.advisory_file_lock(lock_path, timeout=0):
        rc = scheduled_nightly.main()

    assert rc == 1
    report = _report_text(reports)
    assert "maintenance lock contended" in report
    assert str(lock_path) in report


def test_nightly_lock_setup_error_is_logged_and_returns_nonzero(nightly_env, monkeypatch):
    scheduled_nightly, reports = nightly_env

    @contextmanager
    def fail_lock(*_args, **_kwargs):
        raise OSError("lock setup failed")
        yield

    monkeypatch.setattr(scheduled_nightly, "advisory_file_lock", fail_lock, raising=False)
    monkeypatch.setattr(scheduled_nightly, "_run_step", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        scheduled_nightly.maybe_compile,
        "status",
        lambda: {
            "compile_running": False,
            "reason": "compile lock idle",
            "pending_work": False,
        },
    )
    monkeypatch.setattr(scheduled_nightly, "load_state", lambda: {"last_compile_status": "ok"})
    monkeypatch.setattr(
        scheduled_nightly,
        "queue_status",
        lambda: {"pending_total": 0, "permanently_failed": 0, "by_type": {}},
    )

    rc = scheduled_nightly.main()

    assert rc == 1
    report = _report_text(reports)
    assert "maintenance lock unavailable" in report
    assert "OSError: lock setup failed" in report


def test_advisory_lock_does_not_misreport_descriptor_error_as_contention(tmp_path, monkeypatch):
    import memory_state

    def fail_descriptor(_handle):
        raise OSError(errno.EIO, "descriptor failed")

    monkeypatch.setattr(memory_state, "_lock_file_descriptor", fail_descriptor)

    with pytest.raises(OSError, match="descriptor failed"):
        with memory_state.advisory_file_lock(
            tmp_path / "maintenance.lock",
            timeout=0,
            description="maintenance lock",
        ):
            pass


def test_fast_detached_compile_failure_is_reported_and_returns_nonzero(nightly_env, monkeypatch):
    scheduled_nightly, reports = nightly_env
    calls: list[str] = []

    def run_step(_cmd, _log, label, timeout):
        calls.append(label)
        return 0

    monkeypatch.setattr(scheduled_nightly, "_run_step", run_step)
    monkeypatch.setattr(
        scheduled_nightly.maybe_compile,
        "status",
        lambda: {
            "compile_running": False,
            "reason": "compile lock idle",
            "pending_work": True,
        },
    )
    states = iter(
        [
            {"last_compile_status": "ok", "last_compile_started_at": "before"},
            {
                "last_compile_status": "error",
                "last_compile_started_at": "after",
                "last_compile_error": "compile_failed: Luna unavailable",
            },
        ]
    )
    final_state = {
        "last_compile_status": "error",
        "last_compile_started_at": "after",
        "last_compile_error": "compile_failed: Luna unavailable",
    }
    monkeypatch.setattr(
        scheduled_nightly,
        "load_state",
        lambda: next(states, final_state),
        raising=False,
    )
    monkeypatch.setattr(
        scheduled_nightly,
        "queue_status",
        lambda: {"pending_total": 0, "permanently_failed": 0, "by_type": {}},
        raising=False,
    )

    rc = scheduled_nightly.main()

    assert rc == 1
    assert calls == ["drain", "maybe_compile", "lint", "search", "graph"]
    report = _report_text(reports)
    assert "compile status: error" in report
    assert "Luna unavailable" in report
    assert "pending compile work remains" in report


def test_clean_noop_ignores_stale_compile_error(nightly_env, monkeypatch):
    scheduled_nightly, reports = nightly_env
    calls: list[str] = []
    stale_state = {
        "last_compile_status": "error",
        "last_compile_started_at": "2026-07-25T01:00:00",
        "last_compile_finished_at": "2026-07-25T01:00:01",
        "last_compile_error": "historical failure",
    }
    monkeypatch.setattr(
        scheduled_nightly,
        "_run_step",
        lambda _cmd, _log, label, timeout: calls.append(label) or 0,
    )
    monkeypatch.setattr(
        scheduled_nightly.maybe_compile,
        "status",
        lambda: {
            "compile_running": False,
            "reason": "compile lock idle",
            "pending_work": False,
        },
    )
    monkeypatch.setattr(scheduled_nightly, "load_state", lambda: dict(stale_state))
    monkeypatch.setattr(
        scheduled_nightly,
        "queue_status",
        lambda: {"pending_total": 0, "permanently_failed": 0, "by_type": {}},
    )

    rc = scheduled_nightly.main()

    assert rc == 0
    assert calls == ["drain", "maybe_compile", "lint", "search", "graph"]
    report = _report_text(reports)
    assert "historical failure" not in report
    assert "failures=0" in report


def test_sdk_queue_left_pending_is_reported_and_returns_nonzero(nightly_env, monkeypatch):
    scheduled_nightly, reports = nightly_env
    calls: list[str] = []
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "opencode-sdk")
    monkeypatch.setattr(
        scheduled_nightly,
        "_run_step",
        lambda _cmd, _log, label, timeout: calls.append(label) or 0,
    )
    monkeypatch.setattr(
        scheduled_nightly.maybe_compile,
        "status",
        lambda: {
            "compile_running": False,
            "reason": "compile lock idle",
            "pending_work": False,
        },
    )
    monkeypatch.setattr(
        scheduled_nightly,
        "load_state",
        lambda: {"last_compile_status": "ok"},
        raising=False,
    )
    monkeypatch.setattr(
        scheduled_nightly,
        "queue_status",
        lambda: {
            "pending_total": 1,
            "permanently_failed": 0,
            "by_type": {"compile": 1},
        },
        raising=False,
    )

    rc = scheduled_nightly.main()

    assert rc == 1
    assert calls == ["maybe_compile", "lint", "search", "graph"]
    report = _report_text(reports)
    assert "queue still pending: 1 task(s)" in report
    assert "compile=1" in report


def test_processing_only_queue_is_reported_and_returns_nonzero(
    nightly_env, monkeypatch
):
    scheduled_nightly, reports = nightly_env
    monkeypatch.setattr(scheduled_nightly, "_run_step", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        scheduled_nightly.maybe_compile,
        "status",
        lambda: {
            "compile_running": False,
            "reason": "compile lock idle",
            "pending_work": False,
        },
    )
    monkeypatch.setattr(
        scheduled_nightly,
        "load_state",
        lambda: {"last_compile_status": "ok"},
    )
    monkeypatch.setattr(
        scheduled_nightly,
        "queue_status",
        lambda: {
            "pending_total": 0,
            "by_type": {},
            "in_flight": 1,
            "in_flight_by_type": {"query": 1},
            "outstanding_total": 1,
            "permanently_failed": 0,
        },
    )

    assert scheduled_nightly.main() == 1
    report = _report_text(reports)
    assert "queue still outstanding: 1 task(s)" in report
    assert "pending=0" in report
    assert "in_flight=1" in report
    assert "query=1" in report
