"""History is pruned by default; only authority families are kept (audit 2026-09-26 C-11).

docs/research/2026-09-26-a-new-operation-family-is-bounded-by-default.md
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import KEPT_OPERATION_FAMILIES, MarkdownCoordinator
from project_journal import ProjectStore, parse_journal_events

from tests.test_project_journal import checkpoint_event

LATER = timedelta(days=markdown_transaction.HISTORY_RETENTION_DAYS + 1)


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    return MarkdownCoordinator(root, tmp_path / "state")


def _append(coordinator: MarkdownCoordinator, family: str) -> None:
    markdown_transaction._append_until_committed(
        coordinator,
        f"{family}:2026-01-01",
        "knowledge/daily/2026-01-01.md",
        b"# 2026-01-01\n",
        deadline=time.monotonic() + 60,
        cancelled=None,
    )


def _prune_past_window(coordinator: MarkdownCoordinator) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    coordinator.prune(now=now + timedelta(days=3))
    return coordinator.prune_history(now=now + LATER)


@pytest.mark.parametrize("family", ["session-evidence", "project", "a-family-nobody-listed"])
def test_a_family_that_is_not_authority_is_pruned(tmp_path: Path, family: str) -> None:
    coordinator = _coordinator(tmp_path)
    _append(coordinator, family)

    assert _prune_past_window(coordinator)["transactions"] == 1


@pytest.mark.parametrize("family", KEPT_OPERATION_FAMILIES)
def test_an_authority_family_is_kept(tmp_path: Path, family: str) -> None:
    coordinator = _coordinator(tmp_path)
    _append(coordinator, family)

    assert _prune_past_window(coordinator)["transactions"] == 0


def test_a_committed_checkpoint_lets_go_of_its_transaction_and_still_rebuilds(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    store = ProjectStore(vault, tmp_path / "state")
    store.checkpoint("demo", checkpoint_event("evt-1", "rb:event-1"), "agent-a")

    pruned = _prune_past_window(store.coordinator)
    with sqlite3.connect(store.coordinator.database_path) as database:
        named = database.execute("SELECT transaction_id FROM project_checkpoints").fetchall()
    shutil.rmtree(vault / "knowledge/projects/demo")
    store.rebuild_journal("demo")
    journal = (vault / "knowledge/projects/demo/journal.md").read_bytes()

    assert (pruned["transactions"], named) == (1, [(None,)])
    assert [event["sequence"] for event in parse_journal_events("demo", journal)] == [1]
