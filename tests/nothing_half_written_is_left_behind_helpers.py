"""Helpers shared by test_nothing_half_written_is_left_behind.py and the test files importing them."""
from __future__ import annotations

import uuid
from pathlib import Path

from llm_wiki import markdown_undo
from markdown_transaction import MarkdownChange, MarkdownCoordinator

_PAGE = "knowledge/notes/a-page.md"


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return MarkdownCoordinator(root, tmp_path / "state")


def _committed(coordinator: MarkdownCoordinator) -> str:
    record = coordinator.prepare(
        [MarkdownChange.create(_PAGE, b"one\n")], operation_id="a-write"
    )
    coordinator.apply(record.id)
    return record.id


def _kill_a_prune(coordinator: MarkdownCoordinator, transaction_id: str) -> Path:
    """Exactly what `_prune_one` leaves when the process dies after its rename."""
    staged = coordinator.transaction_root / (
        f".{transaction_id}{markdown_undo.STAGED_PRUNE_MARK}{uuid.uuid4().hex}"
    )
    (coordinator.transaction_root / transaction_id).replace(staged)
    return staged
