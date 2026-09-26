"""The scheduler check says what the night could not do.

The code update outcome lived only in the night's log, the installed units on this
machine had no time limit, and the systemd limit sat below the pass's worst case in
auto provider mode. See
docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md.
"""

from __future__ import annotations

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


def test_units_older_than_the_release_are_named(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _unit(tmp_path, "nightly", None)
    _unit(tmp_path, "weekly", install_control.SYSTEMD_START_LIMITS["weekly"])

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
    assert install_control.SYSTEMD_START_LIMITS == {kind: f"{value}h" for kind, value in hours.items()}
