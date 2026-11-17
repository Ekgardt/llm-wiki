"""An answer is fresh by its own sources, not by every file in the vault (audit 2026-09-27 B-10).

A project's `state.md` is rewritten by sessions all day; it made every answer
stale with confidence 0.6. See
docs/research/2026-09-27-an-answer-is-fresh-by-its-own-sources.md.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import mcp_server
import pytest

PAST = 1_000_000_000
GENERATION = "generation-own-sources"
NOTE = "knowledge/notes/a.md"
STATE = "knowledge/projects/demo/state.md"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    root, state = tmp_path / "vault", tmp_path / "state"
    for relative in (NOTE, STATE):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(f"# {relative}\n", encoding="utf-8")
    generation = state / "cache" / "evidence-graph" / "generations" / GENERATION
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_text("{}", encoding="utf-8")
    sources = [{"relative_path": rel, "sha256": hashlib.sha256((root / rel).read_bytes()).hexdigest()} for rel in (NOTE, STATE)]
    (generation / "source-manifest.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
    for path in [*root.rglob("*"), *generation.iterdir()]:
        os.utime(path, (PAST, PAST))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    mcp_server.recorded_memory_digests.cache_clear()
    return root


def _recall(paths: list[str]) -> dict:
    return {
        "results": [{"path": path} for path in paths],
        "retrieval_trace": {"corpus_generation": GENERATION, "requested_mode": "HYBRID", "signals_used": ["lexical"]},
    }


def test_an_uncited_project_state_rewrite_leaves_the_answer_fresh(vault: Path) -> None:
    (vault / STATE).write_text("# rewritten by a session\n", encoding="utf-8")

    components = mcp_server.recall_components(_recall([NOTE]))

    assert (components["lexical"]["freshness"], components["dense"]["freshness"]) == ("fresh", "missing")


def test_a_cited_project_state_rewrite_makes_the_answer_stale(vault: Path) -> None:
    (vault / STATE).write_text("# rewritten by a session\n", encoding="utf-8")

    assert mcp_server.recall_components(_recall([STATE]))["lexical"]["freshness"] == "stale"


def test_a_cited_source_that_vanished_makes_the_answer_stale(vault: Path) -> None:
    (vault / STATE).unlink()

    assert mcp_server.answer_freshness(GENERATION, [STATE]) == "stale"


def test_a_note_compiled_after_the_build_still_makes_any_answer_stale(vault: Path) -> None:
    (vault / "knowledge/notes/new.md").write_text("# New\n", encoding="utf-8")

    assert mcp_server.answer_freshness(GENERATION, [STATE]) == "stale"


def test_an_unreadable_source_manifest_is_unknown_not_fresh(vault: Path, tmp_path: Path) -> None:
    (tmp_path / "state/cache/evidence-graph/generations" / GENERATION / "source-manifest.json").unlink()

    assert mcp_server.answer_freshness(GENERATION, [NOTE]) == "unknown"
