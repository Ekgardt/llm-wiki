"""Helpers shared by test_transaction_abort.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import markdown_transaction
import operational_ownership
import queue_core
from markdown_transaction import MarkdownChange


def _abort_fixture(tmp_path: Path):
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "replace.md").write_bytes(b"replace-before")
    (notes / "delete.md").write_bytes(b"delete-before")
    candidate = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    coordinator = markdown_transaction.MarkdownCoordinator.from_v3_candidate(
        candidate, state_root=tmp_path
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_id = "f" * 64
    owner = registry.acquire(
        "repair", scope="repair:transaction-abort", actor_id="abort-repair"
    )
    fence = coordinator.acquire_intent_fence(intent_id, mode="operator", owner=owner)
    binding = queue_core.CaptureTaskBinding(
        task_id="abort-task",
        intent_id=intent_id,
        intent_sha256="1" * 64,
        handler_version=1,
        active_digest="2" * 64,
        seal_digest="3" * 64,
    )
    coordinator.project_capture_binding(binding, intent_fence=fence)
    record = coordinator.prepare(
        [
            MarkdownChange.create("knowledge/notes/create.md", b"create-after"),
            MarkdownChange.replace("knowledge/notes/replace.md", b"replace-after"),
            MarkdownChange.delete("knowledge/notes/delete.md"),
        ],
        operation_id="abort-mixed-fixture",
    )
    plan = coordinator._load_verified_plan(record)
    rows = coordinator._operation_rows(record.id)
    with coordinator.writer_gate():
        for row, operation_plan in zip(rows, plan["operations"], strict=True):
            coordinator._mutate_and_mark(record.id, row, operation_plan)
    with coordinator._connect() as database:
        database.execute(
            'UPDATE "transaction" SET state="applying" WHERE id=?', (record.id,)
        )
    return coordinator, registry, owner, fence, binding, record
