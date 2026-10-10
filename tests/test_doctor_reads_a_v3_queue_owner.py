"""Doctor reads a live owner of the adopted queue by the v3 names.

The v3 `queue_ownership` row says `owner_token`, `process_id` and `domain_role`;
doctor read `token`, `pid` and `role`, raised IndexError and printed no report
whenever a worker held the queue. The installer's smoke then aborted the update
with "Doctor did not return valid JSON" (2026-09-28, and named on 2026-09-29 once
the smoke carried doctor's stderr). A real worker lease stands in here.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import doctor
from memory_queue import active_memory_queue

from tests.test_a_check_names_its_cause import adopted  # noqa: F401  (fixture)


def test_a_live_v3_worker_is_counted_and_the_check_reports(adopted: tuple[Path, Path]) -> None:  # noqa: F811
    root, state_root = adopted
    queue = active_memory_queue(root, state_root)

    with queue.queue_owner(role="queue-worker", scope="worker:capture-recovery"):
        check = doctor._queue_check(state_root, datetime.now(timezone.utc), time.monotonic() + 60)

    assert (check["id"], check["details"]["live_workers"]) == ("queue", 1)


def _raise_index_error() -> dict:
    raise IndexError("No item with that key")


def test_a_check_that_raises_is_its_own_named_error_not_a_missing_report() -> None:
    check = doctor._isolated("queue", _raise_index_error)

    assert (check["id"], check["status"], check["message"]) == (
        "queue",
        "error",
        "The queue check could not finish: IndexError: No item with that key",
    )


def test_every_check_is_named_by_the_id_it_reports(adopted: tuple[Path, Path]) -> None:  # noqa: F811
    """The isolation names a check by the id the list gives it; a wrong one would mislabel."""
    root, state_root = adopted
    report = doctor.run_doctor(root=root, state_root=state_root, home=root.parent)

    ids = [check["id"] for check in report["checks"]]
    assert ids[:8] == [
        "environment", "runtime", "adoption", "filesystem", "transactions", "queue", "archives", "claims"
    ]
