"""The history prune keeps every row that shows a quarantine was resolved (audit 2026-09-27 A-6, B-1).

docs/research/2026-09-27-a-prune-keeps-what-resolves-a-quarantine.md
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_wiki import markdown_undo
from markdown_transaction import MarkdownCoordinator

from tests.a_prune_keeps_what_resolves_a_quarantine_helpers import (
    _append,
    _quarantine,
    _sql,
    _unresolved,
    _vault,
)

LATER = timedelta(days=markdown_undo.HISTORY_RETENTION_DAYS + 1)


def _each_kind_of_resolution(coordinator: MarkdownCoordinator) -> None:
    """A retry of the same request, a child on the parent chain, a commit of the same created file."""
    _quarantine(coordinator, _append(coordinator, "post-tool:request-one", "2026-01-01"))
    _append(coordinator, "post-tool:request-one:cas:1", "2026-01-01")
    refused = _append(coordinator, "post-tool:request-two", "2026-01-02")
    _quarantine(coordinator, refused)
    child = _append(coordinator, "post-tool:request-two-fixed", "2026-01-02")
    _sql(coordinator, 'UPDATE "transaction" SET parent_transaction_id = ? WHERE id = ?',
         (refused.id, child.id))
    _quarantine(coordinator, _append(coordinator, "post-tool:request-three", "2026-01-03"))
    (coordinator.vault / "knowledge/daily/2026-01-03.md").unlink()
    _append(coordinator, "post-tool:request-three-again", "2026-01-03")


def test_pruning_history_never_reopens_a_resolved_quarantine(tmp_path: Path) -> None:
    coordinator = _vault(tmp_path)
    _each_kind_of_resolution(coordinator)
    _append(coordinator, "post-tool:unrelated", "2026-01-04")
    coordinator.prune(now=datetime.now(UTC) + timedelta(days=3))
    before = _unresolved(coordinator)

    pruned = coordinator.prune_history(now=datetime.now(UTC) + LATER)

    assert (before, _unresolved(coordinator), pruned["transactions"]) == (0, 0, 1)


def test_a_prune_past_its_deadline_keeps_what_it_did_and_says_so(tmp_path: Path) -> None:
    coordinator = _vault(tmp_path)
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _append(coordinator, f"post-tool:{day}", day)
    coordinator.prune(now=datetime.now(UTC) + timedelta(days=3))

    expired = coordinator.prune_history(now=datetime.now(UTC) + LATER, deadline=time.monotonic() - 1)
    sliced = coordinator.prune_history(now=datetime.now(UTC) + LATER, deadline=time.monotonic() + 60,
                                       slice_seconds=0.0)

    assert (expired["transactions"], expired["unfinished"], sliced["transactions"], sliced["unfinished"]) == (
        0, True, 3, False
    )
