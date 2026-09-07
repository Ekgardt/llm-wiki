"""A checkpoint a live writer holds must survive the repair that clears wedges.

A reserved checkpoint has no transaction, so the age of its transaction is the
age of nothing, and `_age_seconds` answers infinity. That made every
reservation look orphaned the instant it was taken — including one a live
writer was in the middle of. The lease settles it exactly: a reservation whose
token is still in `project_leases` and has not expired belongs to a writer that
is alive.

This matters now because the repair moved from a hand-run rescue to a nightly
step: on 2026-09-07 `llm-wiki` 2214 lost a precondition during the benchmark
runs, 2215 sat reserved behind it, `no-hands` 830 likewise, and six hundred
hook failures accumulated over a day, one per session end.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import repair_orphaned_checkpoint_names as repair  # noqa: E402

BATCH = repair.BATCH_NAME_PREFIX + "32a76d16c2"
LEGACY = "event:abcdef0123"
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _row(**overrides):
    record = {
        "occurrence_id": BATCH,
        "lease_token": "",
        "created_at": _stamp(NOW - timedelta(hours=4)),
    }
    record.update(overrides)
    return record


def test_a_reservation_a_live_writer_holds_is_left_alone():
    live = frozenset({"a-live-token"})

    assert not repair._is_orphaned(
        _row(lease_token="a-live-token", created_at=None), NOW.timestamp(), live
    )


def test_a_reservation_whose_lease_is_gone_is_taken():
    assert repair._is_orphaned(
        _row(lease_token="an-expired-token", created_at=None),
        NOW.timestamp(),
        frozenset(),
    )


def test_a_fresh_batch_row_is_left_alone_until_it_ages():
    fresh = _row(created_at=_stamp(NOW - timedelta(minutes=5)))

    assert not repair._is_orphaned(fresh, NOW.timestamp(), frozenset())


def test_an_aged_batch_row_is_taken():
    aged = _row(created_at=_stamp(NOW - timedelta(minutes=31)))

    assert repair._is_orphaned(aged, NOW.timestamp(), frozenset())


def test_a_name_no_request_can_bear_is_taken_whatever_its_age():
    """The August renaming left rows no future request can produce."""
    fresh = _row(occurrence_id=LEGACY, created_at=_stamp(NOW))

    assert repair._is_orphaned(fresh, NOW.timestamp(), frozenset())


def test_a_live_lease_outranks_even_a_name_nothing_can_bear():
    live = frozenset({"held"})
    fresh = _row(occurrence_id=LEGACY, lease_token="held", created_at=_stamp(NOW))

    assert not repair._is_orphaned(fresh, NOW.timestamp(), live)


def test_only_unexpired_leases_count_as_live():
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE project_leases (lease_token TEXT, expires_at TEXT)")
    database.executemany(
        "INSERT INTO project_leases VALUES (?, ?)",
        [
            ("still-held", _stamp(datetime.now(timezone.utc) + timedelta(hours=1))),
            ("long-gone", _stamp(datetime.now(timezone.utc) - timedelta(hours=1))),
        ],
    )

    assert repair.live_lease_tokens(database) == frozenset({"still-held"})


def test_the_nightly_runs_it():
    """It stopped being a rescue somebody has to remember to run."""
    import scheduled_nightly

    labels = [step.label for step in scheduled_nightly._post_compile_steps()]

    assert "checkpoints" in labels
