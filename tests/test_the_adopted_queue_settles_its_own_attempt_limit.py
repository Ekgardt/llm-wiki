"""One attempt limit, settled by the queue, refused by name when a caller names its own.

`claim` used to take the caller's `max_attempts` while `fail` refused it, so a
worker started with `--max-attempts 2` on an adopted vault claimed a task and
then could not record its failure; the task waited out its whole lease instead.
"""

from __future__ import annotations

from pathlib import Path

import memory_queue
import pytest

from tests.adopted_vault import adopt

_SETTLED = memory_queue.DEFAULTS.queue_max_attempts


def _refusal(call) -> tuple[str, bool]:
    """The refusal code, and whether it names the option that was refused."""
    with pytest.raises(memory_queue.QueueOperationError) as raised:
        call()
    return (raised.value.code, "max_attempts" in str(raised.value))


def test_claiming_with_a_caller_s_own_limit_is_refused_by_name(tmp_path: Path) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    queue.enqueue("compile", 1, {"daily": "one"})

    refused = _refusal(lambda: queue.claim("worker", max_attempts=_SETTLED - 1))

    assert refused == ("queue_api_not_adopted", True)


def test_the_settled_limit_still_claims(tmp_path: Path) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    queue.enqueue("compile", 1, {"daily": "one"})

    lease = queue.claim("worker", max_attempts=_SETTLED)

    assert lease is not None


def test_a_worker_told_a_limit_the_queue_refuses_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    queue.enqueue("compile", 1, {"daily": "one"})
    monkeypatch.setattr(memory_queue, "_vault_root", lambda: root)
    monkeypatch.setattr(memory_queue, "_state_root", lambda: state_root)

    refused = _refusal(
        lambda: memory_queue.run_worker(
            lambda _task: True, max_tasks=1, max_attempts=_SETTLED - 1
        )
    )

    # Refused before anything was claimed: the task is still waiting.
    assert (refused, queue.list_tasks()[0].state) == (
        ("queue_api_not_adopted", True),
        "ready",
    )
