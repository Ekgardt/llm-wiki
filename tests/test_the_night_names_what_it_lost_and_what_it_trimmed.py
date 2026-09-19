"""A deferred compile is counted, the scheduler logs are bounded, and the
daily archiver actually runs.

Three promises the scheduled passes did not keep: a compile the pass defers is
stopped with the unit under systemd and the loss was never reported (M-B4); the
logs a scheduler redirects a pass into are named by nothing and grew for the
life of the install (I-A15); and "Archives keep 90 hot days" had no scheduler
behind it at all (M-D4). Research:
`docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import maintenance_helpers  # noqa: E402
import memory_state  # noqa: E402
import scheduled_nightly  # noqa: E402
import scheduled_weekly  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(parents=True)
    monkeypatch.setattr(memory_state, "STATE_DIR", directory)
    monkeypatch.setattr(memory_state, "STATE_FILE", directory / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", directory / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    return directory


def _state(**fields) -> None:
    memory_state.update_state(lambda state: state.update(fields))


def test_a_deferred_compile_that_never_finished_is_reported_once(state_dir) -> None:
    lines: list[str] = []
    _state(last_compile_started_at="2026-09-18T03:10:00", last_compile_status="running")

    scheduled_nightly._remember_deferred_compile(lines.append)
    first = scheduled_nightly._report_deferred_loss(lines.append)
    second = scheduled_nightly._report_deferred_loss(lines.append)

    assert (first, second) == (1, 0)
    assert "never finished" in lines[0]


def test_a_deferred_compile_that_did_finish_is_not_a_failure(state_dir) -> None:
    lines: list[str] = []
    _state(last_compile_started_at="2026-09-18T03:10:00", last_compile_status="running")
    scheduled_nightly._remember_deferred_compile(lines.append)
    _state(last_compile_status="ok")

    assert scheduled_nightly._report_deferred_loss(lines.append) == 0
    assert lines == []


def test_nothing_is_reported_when_no_compile_was_deferred(state_dir) -> None:
    assert scheduled_nightly._report_deferred_loss(print) == 0


def test_a_scheduler_log_keeps_its_tail_and_the_file_it_is_written_to(
    tmp_path, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", logs)
    path = logs / "cron-nightly.log"
    path.write_bytes(b"old\n" * 1000 + b"the last line\n")
    inode = path.stat().st_ino

    dropped = maintenance_helpers.trim_scheduler_logs(keep_bytes=64)

    assert (dropped, path.stat().st_size, path.stat().st_ino) == (4014 - 64, 64, inode)
    assert path.read_bytes().endswith(b"the last line\n")


def test_a_small_scheduler_log_is_left_alone(tmp_path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", logs)
    path = logs / "scheduled-weekly.log"
    path.write_bytes(b"short\n")

    assert maintenance_helpers.trim_scheduler_logs(keep_bytes=1024) == 0
    assert path.read_bytes() == b"short\n"


def test_an_absent_scheduler_log_is_not_created(tmp_path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", logs)

    assert maintenance_helpers.trim_scheduler_logs() == 0
    assert list(logs.iterdir()) == []


def test_the_weekly_pass_archives_the_daily_logs_past_the_hot_window() -> None:
    steps = scheduled_weekly._script_steps()
    commands = {label: command for _message, label, command, _timeout in steps}

    assert "daily_archive" in commands
    assert (Path(commands["daily_archive"][-2]).name, commands["daily_archive"][-1]) == (
        "archive_daily.py",
        "--commit",
    )
