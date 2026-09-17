"""The scheduler's own log is inside the maintenance retention, like every other report.

`logs/scheduled-*.log` (launchd) and `logs/cron-*.log` were appended to for the life of the
vault and named by no retention pattern.

Research: `docs/research/2026-09-17-the-scheduler-log-is-bounded-and-the-hard-kill-is-named.md`.
"""
from __future__ import annotations

import re
from pathlib import Path

import install_control
import maintenance_helpers
import pytest

ROOT, STATE, UV = Path("/vault"), Path("/state"), Path("/usr/bin/uv")


def _log_names(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_.-]+\.log", text))


def _rendered_log_names() -> set[str]:
    """Every log file name the two POSIX backends write, read from what they render."""
    plists = [install_control._launchd_job(ROOT, STATE, UV, kind) for kind in ("nightly", "weekly")]
    cron = install_control.render_cron_block(ROOT, STATE, UV)
    return _log_names(b"".join((*plists, cron)).decode("utf-8"))


def _covered(name: str) -> bool:
    return any(Path(name).match(pattern) for pattern in maintenance_helpers.MAINTENANCE_REPORT_PATTERNS)


def test_every_scheduler_log_the_installer_renders_is_pruned() -> None:
    names = _rendered_log_names()

    uncovered = sorted(name for name in names if not _covered(name))

    assert (sorted(names), uncovered) == (
        ["cron-nightly.log", "cron-weekly.log", "scheduled-nightly.log", "scheduled-weekly.log"],
        [],
    )


@pytest.fixture
def reports(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", directory)
    monkeypatch.setattr(maintenance_helpers, "ARTIFACT_DIR", directory / "maintenance")
    return directory


def test_a_scheduler_log_over_the_family_size_is_taken(reports: Path) -> None:
    """The product path: the nightly's own prune, with the real retention rules."""
    (reports / "cron-nightly.log").write_bytes(b"x" * (maintenance_helpers.REPORT_RETENTION_BYTES + 1))
    (reports / "cron-weekly.log").write_bytes(b"y" * 16)

    removed = maintenance_helpers.prune_maintenance_output()

    assert (removed, (reports / "cron-nightly.log").exists()) == (1, False)
