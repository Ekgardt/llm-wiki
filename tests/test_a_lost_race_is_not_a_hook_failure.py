"""A writer that lost a race did not fail: the next session carries the event.

Measured on this vault on 2026-09-07: `no-hands` logged four checkpoint errors
in four minutes — `owner_busy` and `operation_id is already bound to a
different request` — while its committed sequence advanced from 836 to 838. No
checkpoint was lost. Every one of those lines was a session that arrived while
another held the project, and the event went in on the next attempt.

Counting them as failures made the hooks check permanently red on a machine
that runs several agents, and a check that is always red stops being read. The
lines are still written and still counted in the details; what changed is that
they no longer decide the status.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doctor  # noqa: E402
import integration_adapter  # noqa: E402

NOW = datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc)
JUST_NOW = (NOW - timedelta(seconds=30)).isoformat()


def _details(kinds: dict, last_at: str = JUST_NOW) -> dict:
    return {"kinds": kinds, "last_at": last_at, "recent": sum(kinds.values())}


def test_a_lost_race_is_written_under_its_own_name():
    from markdown_transaction import OperationBoundElsewhereError, ProjectPendingPriorError
    from operational_ownership import OperationalOwnershipError

    for error in (
        OperationalOwnershipError("owner_busy"),
        ProjectPendingPriorError("x", 9, 8),
        OperationBoundElsewhereError("operation_id is already bound to a different request"),
    ):
        assert (
            integration_adapter._checkpoint_log_kind(error)
            == "project checkpoint contention"
        )


def test_a_state_lock_timeout_is_contention_too():
    """The event is queued in project_checkpoint_pending and drained later."""
    from memory_state import StateLockTimeout

    error = StateLockTimeout("Could not acquire state lock: run/state.json.lock")

    assert (
        integration_adapter._checkpoint_log_kind(error)
        == "project checkpoint contention"
    )


def test_an_ownership_refusal_that_is_not_a_race_keeps_the_plain_name():
    from operational_ownership import OperationalOwnershipError

    assert (
        integration_adapter._checkpoint_log_kind(
            OperationalOwnershipError("owner_record_invalid")
        )
        == "project checkpoint"
    )


def test_the_trail_already_written_is_classified_by_what_it_says():
    """Six hundred lines predate the naming; reading the message covers them."""
    lines = [
        "[2026-09-07T15:53:56] project checkpoint: OperationalOwnershipError: owner_busy",
        "[2026-09-07T12:49:45] project checkpoint: TimeoutError: Could not acquire state lock: x",
        "[2026-09-07T11:00:00] project checkpoint: KeyError: 'project'",
    ]

    kinds = doctor._hook_error_kinds(doctor._hook_error_records(lines))

    assert kinds == {"project checkpoint contention": 2, "project checkpoint": 1}


def test_a_real_failure_keeps_the_plain_name():
    assert (
        integration_adapter._checkpoint_log_kind(KeyError("project"))
        == "project checkpoint"
    )


def test_contention_alone_does_not_degrade_the_check():
    details = _details({"project checkpoint contention": 603})

    result = doctor._hook_error_result(True, 603, details)

    assert result["status"] == "ok"
    assert "603 lost race(s)" in result["message"]
    assert details["kinds"]["project checkpoint contention"] == 603


def test_a_real_failure_still_degrades_the_check():
    details = _details({"project checkpoint contention": 600, "project checkpoint": 3})

    result = doctor._hook_error_result(True, 603, details)

    assert result["status"] == "degraded"
    assert "3 hook failure(s)" in result["message"]


def test_an_old_failure_is_reported_without_degrading():
    details = _details({"project checkpoint": 3}, last_at="2026-09-01T00:00:00")

    result = doctor._hook_error_result(False, 3, details)

    assert result["status"] == "ok"
    assert "none recently" in result["message"]


def test_a_quiet_trail_says_so():
    result = doctor._hook_error_result(False, 0, _details({}))

    assert result["status"] == "ok"
    assert result["message"] == "No hook failure is recorded."
