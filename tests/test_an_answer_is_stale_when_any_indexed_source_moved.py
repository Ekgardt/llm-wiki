"""An answer is stale when anything its index holds has moved since it was built.

See docs/research/2026-09-25-an-answer-is-stale-when-any-indexed-source-moved.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import mcp_server
import memory_state
import pytest

PAST = 1_000_000_000


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(memory_state, "ROOT", tmp_path)
    for relative in ("knowledge/notes/a.md", "knowledge/notes/b.md", "knowledge/projects/demo/state.md",
                     "knowledge/projects/demo/journal.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    for path in tmp_path.rglob("*"):
        os.utime(path, (PAST, PAST))
    return tmp_path


def test_a_deleted_note_moves_the_newest_source(vault: Path) -> None:
    before = mcp_server._newest_page_ns()
    (vault / "knowledge/notes/b.md").unlink()

    assert mcp_server._newest_page_ns() > before


def test_a_project_state_change_moves_it_and_a_journal_change_does_not(vault: Path) -> None:
    before = mcp_server._newest_page_ns()
    (vault / "knowledge/projects/demo/journal.md").write_text("more\n", encoding="utf-8")
    after_journal = mcp_server._newest_page_ns()
    (vault / "knowledge/projects/demo/state.md").write_text("changed\n", encoding="utf-8")

    assert (after_journal == before, mcp_server._newest_page_ns() > before) == (True, True)


def test_a_renamed_note_moves_the_newest_source(vault: Path) -> None:
    before = mcp_server._newest_page_ns()
    (vault / "knowledge/notes/a.md").rename(vault / "knowledge/notes/c.md")
    os.utime(vault / "knowledge/notes/c.md", (PAST, PAST))

    assert mcp_server._newest_page_ns() > before
