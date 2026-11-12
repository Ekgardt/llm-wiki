"""Search over the vault: title and summary extraction, the bounded page collector, limits, impressions,
telemetry and the status report.

Generation artifacts, fallbacks and publication live in `tests/test_search_ranking_*.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import generation_search
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_extract_title_and_summary():
    """Title from H1, summary from 'One-sentence summary:' line."""

    content = """---
type: concept
---
# My Great Concept

One-sentence summary: This concept is about something important.

## Body
Content here.
"""
    title, summary = generation_search.extract_title_and_summary(content, "fallback")
    assert title == "My Great Concept"
    assert "something important" in summary


def test_extract_title_fallback_to_stem():
    """No H1 → filename stem."""

    content = "Just body, no heading."
    title, summary = generation_search.extract_title_and_summary(content, "my-file")
    assert title == "my-file"
    assert summary == ""


def test_strip_frontmatter():
    """Frontmatter is removed from search body."""

    content = "---\ntype: fact\nsecret: sk-test123\n---\n\n# Real Content\nBody text."
    stripped = generation_search.strip_frontmatter(content)
    assert "sk-test" not in stripped
    assert "Real Content" in stripped


def test_collect_pages_skips_editorial(tmp_path, monkeypatch):
    """Editorial filenames (index.md, log.md) are skipped."""

    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for name in sorted(generation_search.SKIP_NAMES) + ["page.md"]:
        (notes / name).write_text("# Page\n", encoding="utf-8")

    assert generation_search.collect_pages("all") == [notes / "page.md"]


def test_collect_pages_reads_nothing_from_an_absent_vault(tmp_path, monkeypatch):

    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    assert generation_search.collect_pages("all") == []


def test_page_collector_honors_deadline(tmp_path):

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\n", encoding="utf-8")

    with pytest.raises(TimeoutError):
        generation_search.collect_pages(
            knowledge_dir=notes,
            root=tmp_path,
            deadline=time.monotonic() - 1,
        )


@pytest.mark.parametrize("limit_name", ["MAX_SEARCH_ENTRIES", "MAX_SEARCH_DIRECTORIES"])
def test_page_collector_bounds_all_entries_and_directories(tmp_path, monkeypatch, limit_name):

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    if limit_name == "MAX_SEARCH_ENTRIES":
        monkeypatch.setattr(generation_search, "MAX_SEARCH_ENTRIES", 2)
        for index in range(4):
            (notes / f"ignored-{index}.bin").write_bytes(b"x")
    else:
        monkeypatch.setattr(generation_search, "MAX_SEARCH_DIRECTORIES", 2)
        for index in range(4):
            (notes / f"directory-{index}").mkdir()

    with pytest.raises(ValueError, match="limit"):
        generation_search.collect_pages(knowledge_dir=notes, root=tmp_path)


def test_page_collector_bounds_depth_before_reading_deep_page(tmp_path, monkeypatch):

    notes = tmp_path / "knowledge" / "notes"
    deep = notes / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "secret.md").write_text("# Too deep\n", encoding="utf-8")
    monkeypatch.setattr(generation_search, "MAX_SEARCH_DEPTH", 2)

    with pytest.raises(ValueError, match="depth"):
        generation_search.collect_pages(knowledge_dir=notes, root=tmp_path)


def test_slug_is_indexed_and_selects_the_matching_duplicate(tmp_path, monkeypatch):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    first = notes / "first-implementation.md"
    second = notes / "second-implementation.md"
    content = "---\ntype: concept\n---\n# Shared title\n\nUnrelated body.\n"
    first.write_text(content, encoding="utf-8")
    second.write_text(content, encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

    results = search_memory.search("second implementation")

    assert results[0]["path"] == "knowledge/notes/second-implementation.md"


def test_exact_filename_short_circuit_emits_final_impressions_once(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "exact-page.md"
    page.write_text("---\ntype: concept\n---\n# Exact Page\nbody\n", encoding="utf-8")
    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    results = search_memory.search(
        "exact page", graph=False, rerank=False,
        source_tool="test.search",
    )

    rows = retrieval_telemetry.read_events(limit=10, db_path=database)
    assert len(rows) == len(results) == 1
    # One column, one identity: the page's vault-relative path. See
    # `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
    assert (
        rows[0].event_kind,
        rows[0].candidate_id,
        rows[0].rank,
        rows[0].retrieval_mode,
        rows[0].source_tool,
    ) == ("impression", "knowledge/notes/exact-page.md", 1, "exact", "test.search")


def test_empty_search_emits_no_impressions(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    assert search_memory.search("", source_tool="test.search") == []
    assert retrieval_telemetry.read_events(limit=10, db_path=database) == []


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "10"])
def test_search_rejects_invalid_limit_before_dispatch(limit, monkeypatch):
    import search_memory

    monkeypatch.setattr(
        generation_search,
        "active_generation_catalog",
        lambda: pytest.fail("invalid limit reached generation selection"),
    )

    with pytest.raises(ValueError, match="limit"):
        search_memory.search("needle", limit=limit)


def test_cli_rejects_invalid_limit_before_search(monkeypatch):
    import search_memory

    monkeypatch.setattr(sys, "argv", ["search_memory.py", "needle", "--limit", "0"])
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *args, **kwargs: pytest.fail("invalid CLI limit reached search"),
    )

    with pytest.raises(SystemExit) as error:
        search_memory.main()

    assert error.value.code == 2


def test_search_can_defer_telemetry_to_post_filter_caller(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "exact-page.md"
    page.write_text("# Exact Page\n", encoding="utf-8")
    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    assert search_memory.search(
        "exact page", graph=False, rerank=False,
        emit_telemetry=False,
    )
    assert retrieval_telemetry.read_events(limit=10, db_path=database) == []


def test_status_names_the_active_generation(monkeypatch, capsys):
    """Audit M3: the status says what an answer reads."""
    import search_memory

    class _Catalog:
        def get_active(self):
            return {
                "generation_id": "generation-1",
                "extractor_version": "markdown-heading-extractor/v3",
                "vector_state": "complete",
                "embedding_model_id": "intfloat/multilingual-e5-small",
            }

    monkeypatch.setattr(search_memory, "active_generation_catalog", lambda: _Catalog())
    monkeypatch.setattr(search_memory, "collect_pages", lambda _scope: ["one", "two"])

    assert search_memory.print_status() == 0
    lines = capsys.readouterr().out.splitlines()

    assert lines[0] == (
        "Active generation: generation-1 (markdown-heading-extractor/v3, "
        "vectors complete, model intfloat/multilingual-e5-small)"
    )
    assert lines[1] == "Searchable pages (superseded excluded): 2"


def test_status_says_when_there_is_no_generation(monkeypatch, capsys):
    import search_memory

    monkeypatch.setattr(search_memory, "active_generation_catalog", lambda: None)
    monkeypatch.setattr(search_memory, "collect_pages", lambda _scope: [])

    search_memory.print_status()

    assert capsys.readouterr().out.splitlines()[0] == (
        "Active generation: none (search reads Markdown directly; run --rebuild)"
    )


class TestASilentFallbackNamesItsCause:
    """Audit M5/M6: a vector or generation stage that raises records why."""


    def test_an_unreadable_catalog_is_named(self, monkeypatch):
        import retrieval
        import search_memory

        class _Catalog:
            def get_active_for_repository(self, *_a, **_k):
                raise ValueError("catalog.sqlite3: file is not a database")

        generation_search.DEGRADATIONS.clear()
        answer = retrieval.active_manifest_for(
            _Catalog(), deadline=None, cancelled=None, stop={}
        )

        assert answer is None
        assert search_memory.degradation_reasons()["generation_manifest"].startswith(
            "ValueError: catalog.sqlite3"
        )
