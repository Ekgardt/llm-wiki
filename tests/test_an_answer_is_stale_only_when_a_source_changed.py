"""An answer is stale only when a source the generation indexes really changed (audit 2026-09-26 B-15).

docs/research/2026-09-26-an-answer-is-stale-only-when-a-source-changed.md
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import mcp_server
import memory_state
import pytest

PAST = 1_000_000_000
GENERATION = "generation-freshness"
PAGE = "knowledge/notes/a.md"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    root, state = tmp_path / "vault", tmp_path / "state"
    page = root / PAGE
    page.parent.mkdir(parents=True)
    page.write_text("# A\n\nAlpha.\n", encoding="utf-8")
    generation = state / "cache" / "evidence-graph" / "generations" / GENERATION
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_text("{}", encoding="utf-8")
    sources = [{"relative_path": PAGE, "sha256": hashlib.sha256(page.read_bytes()).hexdigest()}]
    (generation / "source-manifest.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
    for path in [*root.rglob("*"), *generation.iterdir()]:
        os.utime(path, (PAST, PAST))
    monkeypatch.setattr(memory_state, "ROOT", root)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state)
    getattr(getattr(mcp_server, "_recorded_memory_digests", None), "cache_clear", lambda: None)()
    return root


def test_a_touched_page_is_not_a_change(vault: Path) -> None:
    os.utime(vault / PAGE)

    assert mcp_server._index_is_behind(GENERATION) is False


def test_an_edited_page_is(vault: Path) -> None:
    (vault / PAGE).write_text("# A\n\nAlpha, edited.\n", encoding="utf-8")

    assert mcp_server._index_is_behind(GENERATION) is True


def test_a_new_page_is_and_a_new_readme_is_not(vault: Path) -> None:
    (vault / "knowledge" / "notes" / "README.md").write_text("# Notes\n", encoding="utf-8")
    readme_only = mcp_server._index_is_behind(GENERATION)
    (vault / "knowledge" / "notes" / "b.md").write_text("# B\n", encoding="utf-8")

    assert (readme_only, mcp_server._index_is_behind(GENERATION)) == (False, True)


def test_a_removed_page_is(vault: Path) -> None:
    (vault / PAGE).unlink()
    os.utime(vault / "knowledge" / "notes")

    assert mcp_server._index_is_behind(GENERATION) is True
