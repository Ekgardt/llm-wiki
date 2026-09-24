"""The weekly pass keeps its own record and doctor reads it.

The weekly failed on 2026-09-13 and 2026-09-20 and nothing said so: its failure
went into the nightly's field, which the next nightly overwrote, and doctor had no
weekly check. See docs/research/2026-09-24-the-weekly-pass-has-its-own-record.md.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402
import scheduled_nightly  # noqa: E402
import scheduled_weekly  # noqa: E402

from tests.test_doctor import (  # noqa: E402
    _build_root,
    _check,
    _nightly_state,
    _qualified_pyright_check,
)

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
FRESH_NIGHTLY = {
    "last_nightly_status": "success",
    "last_nightly_date": "2026-09-27",
    "last_nightly_at": "2026-09-27T03:09:00+00:00",
}


def _scheduler(tmp_path: Path, monkeypatch, weekly: dict) -> dict:
    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)
    _nightly_state(state_root, {**FRESH_NIGHTLY, **weekly})
    report = doctor.run_doctor(root=root, state_root=state_root, home=home, now=NOW)
    return _check(report, "scheduler")


def test_a_failed_weekly_is_an_error_even_after_a_good_nightly(tmp_path, monkeypatch):
    check = _scheduler(tmp_path, monkeypatch, {"last_weekly_status": "failed"})

    assert (check["status"], check["message"]) == ("error", "Last weekly maintenance failed.")


def test_a_weekly_silent_for_more_than_eight_days_is_stale(tmp_path, monkeypatch):
    weekly = {"last_weekly_status": "success", "last_weekly_at": "2026-09-13T04:20:00+00:00"}

    check = _scheduler(tmp_path, monkeypatch, weekly)

    assert (check["status"], check["message"]) == ("degraded", "Weekly maintenance is stale.")


def test_a_recent_weekly_and_nightly_are_current(tmp_path, monkeypatch):
    weekly = {"last_weekly_status": "success", "last_weekly_at": "2026-09-20T04:20:00+00:00"}

    assert _scheduler(tmp_path, monkeypatch, weekly)["status"] == "ok"


def test_a_vault_that_never_ran_a_weekly_is_not_degraded_for_it(tmp_path, monkeypatch):
    assert _scheduler(tmp_path, monkeypatch, {})["status"] == "ok"


def test_the_weekly_writes_its_own_fields_and_never_the_nightlys(monkeypatch):
    state: dict = {"last_nightly_status": "success"}
    monkeypatch.setattr(scheduled_weekly, "update_state", lambda mutate: mutate(state))

    scheduled_weekly.record_weekly_result(1, "boom")
    failed = dict(state)
    scheduled_weekly.record_weekly_result(0)

    assert (failed["last_weekly_status"], failed["last_weekly_failure"]["error"]) == ("failed", "boom")
    assert (state["last_weekly_status"], "last_weekly_failure" in state) == ("success", False)
    assert state["last_nightly_status"] == "success"


def test_a_fence_failure_is_recorded_as_the_weeklys(monkeypatch):
    recorded: list[tuple] = []

    def refuse(_role):
        raise RuntimeError("candidate artifacts remain after adoption")

    monkeypatch.setattr(scheduled_weekly.scheduled_nightly, "take_scheduled_fence", refuse)
    monkeypatch.setattr(scheduled_weekly, "record_weekly_result", lambda *args: recorded.append(args))
    monkeypatch.setattr(
        scheduled_nightly,
        "record_scheduled_failure",
        lambda *_a: pytest.fail("the nightly's record must not be written"),
    )

    with pytest.raises(RuntimeError):
        scheduled_weekly.main()

    assert recorded[0][0] == 1


def test_the_step_log_numbers_steps_in_the_order_they_run():
    lines: list[str] = []
    log = scheduled_nightly.StepLog(lines.append)

    log.step("first")
    log("  detail")
    log.step("second")

    assert lines == ["Step 1: first", "  detail", "Step 2: second"]


def test_no_nightly_or_weekly_message_carries_a_hand_written_number():
    nightly = [scheduled_nightly._capture_adoption_step(), *scheduled_nightly._post_compile_steps()]
    messages = [step.message for step in nightly]
    messages += [message for message, *_rest in scheduled_weekly._script_steps()]

    assert [message for message in messages if message.startswith("Step ")] == []
