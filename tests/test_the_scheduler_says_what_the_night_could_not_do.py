"""The scheduler check says what the night could not do.

The code update outcome lived only in the night's log, the installed units on this
machine had no time limit, and the systemd limit sat below the pass's worst case in
auto provider mode. See
docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import doctor
import install_control
import pytest
import scheduled_nightly


def _unit(home: Path, kind: str, limit: str | None) -> None:
    directory = home / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["[Service]", "Type=oneshot"] + ([f"TimeoutStartSec={limit}"] if limit else [])
    (directory / f"llm-wiki-{kind}.service").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status": "updated", "dependencies": "synced", "resources": "current"}, None),
        ({"status": "skipped", "reason": "not_on_default_branch"}, "not on its default branch"),
        ({"status": "error", "reason": "fetch_failed"}, "could not fetch"),
        ({"status": "updated", "dependencies": "stale", "resources": "current"}, "`uv sync --locked --inexact`"),
        ({"status": "updated", "dependencies": "synced", "resources": "rerun_installer"}, "rerun the installer"),
    ],
)
def test_a_recorded_update_that_needs_the_operator_is_named(record, expected) -> None:
    verdict = doctor._update_verdict(record)

    assert (verdict is None, expected is None or expected in verdict[1]) == (expected is None, True)


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (
            "not_on_default_branch",
            "The last nightly code update was skipped because the vault was not on its default branch.",
        ),
        (
            "diverged_branch",
            "The last nightly code update was skipped because the vault's branch had diverged from the remote.",
        ),
        (
            "local_changes_conflict",
            "The last nightly code update was stopped by a conflicting local change; see the nightly log.",
        ),
    ],
)
def test_a_past_branch_skip_is_not_presented_as_a_current_branch_probe(reason, message) -> None:
    verdict = doctor._update_verdict({
        "status": "skipped", "reason": reason,
        "at": "2026-09-29T12:01:24+00:00",
    })

    assert verdict == ("degraded", message)


def test_units_older_than_the_release_are_named(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _unit(tmp_path, "nightly", None)
    _unit(tmp_path, "weekly", doctor._expected_unit_limit("weekly"))

    verdict = doctor._unit_limit_verdict(tmp_path)

    assert verdict is not None and "nightly" in verdict[1] and "weekly" not in verdict[1]


def test_no_installed_units_is_not_a_finding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert doctor._unit_limit_verdict(tmp_path) is None


def test_the_nightly_keeps_what_the_update_did() -> None:
    record = scheduled_nightly.update_record({"status": "skipped", "reason": "not_on_default_branch", "extra": 1})

    assert (set(record), record["reason"]) == (
        {"status", "reason", "dependencies", "resources", "resources_since", "at"},
        "not_on_default_branch",
    )


def test_one_table_sets_every_scheduler_limit() -> None:
    hours = install_control.SCHEDULER_LIMIT_HOURS

    assert install_control.WINDOWS_TASK_LIMIT_HOURS == hours
    assert install_control.scheduler_limit_hours(Path.cwd()) == hours


@pytest.mark.parametrize("compile_status", ["running", "ok", "failed"])
def test_deferred_maintenance_is_not_reported_as_complete(tmp_path, compile_status) -> None:
    from tests.test_doctor import _build_root

    root, state_root, home = _build_root(tmp_path)
    state = {
        "last_nightly_date": "2026-09-29",
        "last_nightly_status": "success",
        "last_compile_status": compile_status,
        "nightly_deferred_compile": "2026-09-29T13:15:42+00:00",
    }
    path = state_root / "run" / "state.json"
    before = json.dumps(state)
    path.write_text(before, encoding="utf-8")

    result = doctor._scheduler_check(
        root, state_root, datetime(2026, 9, 29, tzinfo=timezone.utc),
        time.monotonic() + 10, home,
    )

    assert result["status"] == "degraded"
    assert "deferred" in result["message"]
    assert "Nightly maintenance is current" not in result["message"]
    assert result["details"]["nightly_deferred_compile"] == state["nightly_deferred_compile"]
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("status", "deferred", "expected"),
    [("failed", "2026-09-29T13:15:42+00:00", "error"), ("success", None, "ok")],
)
def test_deferred_diagnostics_preserve_failure_and_completed_run_status(status, deferred, expected) -> None:
    state = {
        "last_nightly_date": "2026-09-29", "last_nightly_status": status,
        "nightly_deferred_compile": deferred,
    }

    result = doctor._nightly_result(state, datetime(2026, 9, 29, tzinfo=timezone.utc), {})

    assert result["status"] == expected
