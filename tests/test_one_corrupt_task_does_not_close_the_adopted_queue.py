"""One tampered payload is a fact about one task, not about the whole queue.

Every open of the adopted queue used to run the whole-file validation, which
counts a payload mismatch on a live task as a broken database. So one bad row
stopped every hook from enqueueing and every worker from claiming, and the row
could never reach the per-task demotion that was written for it.
"""

from __future__ import annotations

from pathlib import Path

import memory_queue

from tests.adopted_vault import adopt, tamper_payload, task_state


def test_the_queue_still_opens_and_takes_work_beside_a_corrupt_task(
    tmp_path: Path,
) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    bad = queue.enqueue("compile", 1, {"daily": "one"})
    tamper_payload(state_root, bad)

    reopened = memory_queue.active_or_legacy_memory_queue(root, state_root)
    good = reopened.enqueue("compile", 1, {"daily": "two"})
    lease = reopened.claim("worker")

    assert lease is not None
    assert lease.id == good
    assert task_state(state_root, bad) == ("dead", "payload_hash_mismatch")
