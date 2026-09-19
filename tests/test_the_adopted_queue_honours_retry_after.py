"""A failure that says when to come back is obeyed by the adopted queue too.

The legacy queue puts a failed task back no sooner than its `retry_after`. The
adopted queue computed only its own backoff, so a provider's hour-long
Retry-After left the task ready seconds later and every attempt burned at once.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import memory_queue
from memory_queue import QueueFailure

from tests.adopted_vault import adopt


def test_a_rate_limited_task_waits_out_the_stated_hour(tmp_path: Path) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    task_id = queue.enqueue("compile", 1, {"daily": "one"})
    lease = queue.claim("worker")

    queue.fail(lease, QueueFailure("rate_limited", retry_after=3600))

    task = queue.get(task_id)
    assert task.state == "ready"
    assert task.available_at - task.updated_at >= timedelta(seconds=3599)
