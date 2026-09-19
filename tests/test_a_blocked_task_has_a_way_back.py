"""A task blocked on a capability can be put back to work by the operator.

`process_cleanup` is the only capability the queue blocks on, and nothing could
move such a task again: redrive wants `dead`, the doctor unblocks only `llm.*`
capabilities that nothing emits. `cancel` was the only exit, and it loses the work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import memory_queue
import pytest
from memory_queue import QueueFailure

from tests.adopted_vault import adopt


def _cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", *arguments])
    code = memory_queue._cli()
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, state_root = adopt(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return memory_queue.active_or_legacy_memory_queue(root, state_root)


def test_unblock_returns_a_blocked_task_to_the_worker(
    adopted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = adopted.enqueue("compile", 1, {"daily": "one"})
    adopted.fail(
        adopted.claim("worker"),
        QueueFailure("process_cleanup_failed", blocked_capability="process_cleanup"),
    )
    blocked = adopted.get(task_id).state

    result = _cli(monkeypatch, capsys, ["unblock", task_id])
    lease = adopted.claim("worker")

    assert blocked == "blocked"
    assert result == (0, {"id": task_id, "state": "ready"})
    assert lease.id == task_id


def test_unblock_refuses_a_task_that_is_not_blocked(
    adopted, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = adopted.enqueue("compile", 1, {"daily": "one"})

    result = _cli(monkeypatch, capsys, ["unblock", task_id])

    assert result == (2, {"codes": ["unblock_requires_blocked"]})
    assert adopted.get(task_id).state == "ready"
