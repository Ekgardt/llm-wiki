"""Helpers shared by test_queue_v3_capture_links.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import markdown_transaction
import queue_core
from installed_memory_repair import repair_installed_vault

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _queue(tmp_path: Path):
    coordinator = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(
        coordinator, source_v2=None
    )
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)
    return queue_core.MemoryQueue.from_v3_candidate(
        candidate, state_root=tmp_path
    )


def _coordinator(tmp_path: Path):
    return markdown_transaction.MarkdownCoordinator.from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3",
        state_root=tmp_path,
    )


def _adopted_vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    assert report["overall_status"] == "ok"
    return root, state_root


def _capture_binding(
    queue, coordinator, registry, *, intent_id: str, intent_sha256: str
):
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "capture"},
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return binding
