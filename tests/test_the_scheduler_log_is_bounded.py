"""The scheduler's own log is inside the maintenance retention, like every other report.

`logs/scheduled-*.log` (launchd) and `logs/cron-*.log` were appended to for the life of the
vault and named by no retention pattern.

Research: `docs/research/2026-09-17-the-scheduler-log-is-bounded-and-the-hard-kill-is-named.md`.
"""
from __future__ import annotations

import os
import re
import time
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


def _oversized_family(reports: Path) -> tuple[Path, Path]:
    """One scheduler log past the family budget, beside a tiny sibling."""
    big = reports / "cron-nightly.log"
    small = reports / "cron-weekly.log"
    big.write_bytes(b"x" * (maintenance_helpers.REPORT_RETENTION_BYTES + 1))
    small.write_bytes(b"y" * 16)
    return big, small


def test_a_scheduler_log_over_the_family_size_is_taken(reports: Path) -> None:
    """The product path: the nightly's own prune, with the real retention rules."""
    big, _small = _oversized_family(reports)

    removed = maintenance_helpers.prune_maintenance_output()

    assert (removed, big.exists()) == (1, False)


class _NameOrderedDirectory:
    """A directory that lists its entries by name, the way NTFS does.

    NTFS indexes a directory by name, so `scandir` there yields
    `cron-nightly.log` before `cron-weekly.log`; ext4's hash order happens to
    yield the other one first. Nothing in the product may depend on which.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def exists(self) -> bool:
        return self._path.exists()

    def glob(self, pattern: str) -> list[Path]:
        return sorted(self._path.glob(pattern))


def test_two_logs_written_in_one_clock_tick_still_prune_the_same_way(reports: Path) -> None:
    """A tie in mtime is not decided by the listing: the small log outlives the big.

    Windows moves its file-time clock in ~15.6 ms ticks and HFS+ stores whole
    seconds, so a scheduler that redirects two jobs gives both logs one mtime.
    The listing order decided it then, and on NTFS's order this family lost both
    files - two removals where the budget only needed one. Research:
    `docs/research/2026-09-18-a-retention-order-does-not-depend-on-the-clocks-granularity.md`.
    """
    big, small = _oversized_family(reports)
    # Now, truncated to a whole second: the coarsest tick these filesystems
    # have. An older stamp would meet the age rule instead of the size rule.
    tick = float(int(time.time()))
    for path in (big, small):
        os.utime(path, (tick, tick))

    removed = maintenance_helpers.prune_reports(_NameOrderedDirectory(reports), "cron-*.log")

    assert (removed, big.exists(), small.exists()) == (1, False, True)
