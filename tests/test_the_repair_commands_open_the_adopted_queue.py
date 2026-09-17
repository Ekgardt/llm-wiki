"""`quarantine-corrupt` and `purge-corrupt` work on a vault that adopted V3.

Both commands used to open the unpublished candidate queue and the candidate
coordinator. Adoption requires those candidates to be gone, so on every adopted
vault the commands exited 2 with `permission_denied`. They are the only repair
for a corrupt task, so the product path is run here with nothing patched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import markdown_transaction
import memory_queue
import pytest
from reliable_memory import sha256_bytes

from tests.adopted_vault import adopt, tamper_payload


def _dead_corrupt_task(root: Path, state_root: Path) -> str:
    """A capture-bound task whose payload was tampered with, demoted by a claim."""
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    registry = queue.ownership_registry()
    coordinator = markdown_transaction.active_markdown_coordinator(root, state_root)
    intent_id = sha256_bytes(b"intent:corrupt")
    intent = {
        "intent_id": intent_id,
        "intent_path": f"run/capture-intents/{intent_id}.json",
        "intent_sha256": "b" * 64,
    }
    queue.publish_capture_intent(byte_size=128, **intent)
    owner = registry.acquire("capture", scope=f"intent:{intent_id}", actor_id="capture")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task(
        "flush", 1, {"prompt": "corrupt later"}, capture_fence=fence, owner=owner, **intent
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    tamper_payload(state_root, binding.task_id)
    queue.claim_capture("worker")
    return binding.task_id


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", *arguments])
    code = memory_queue._cli()
    return code, json.loads(capsys.readouterr().out)


def test_a_corrupt_task_is_quarantined_and_purged_through_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, state_root = adopt(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    task_id = _dead_corrupt_task(root, state_root)

    quarantine = _run_cli(
        monkeypatch, capsys, ["quarantine-corrupt", task_id, "--reason", "bit rot"]
    )
    purge = _run_cli(monkeypatch, capsys, ["purge-corrupt", task_id])

    assert (quarantine[0], quarantine[1]["task_id"]) == (0, task_id)
    assert quarantine[1]["state"] == "quarantined"
    assert (purge[0], purge[1]["task_id"]) == (0, task_id)
