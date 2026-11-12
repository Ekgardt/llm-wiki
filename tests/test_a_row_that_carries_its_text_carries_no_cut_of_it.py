"""A recall row that carries its text carries no cut of it (audit 2026-09-27 C-16).

The summary was the chunk's first line cut at 120 characters: `content` again, often
mid-sentence. See docs/research/2026-09-27-a-row-that-carries-its-text-carries-no-cut-of-it.md.
"""
from __future__ import annotations

import json

CONTENT = (
    "# The Vault Updates Its Own Code\n\nOne-sentence summary: the nightly pass may advance the checkout to the "
    "remote branch when that is a strict fast-forward and touches no file the owner has modified.\n"
)


def _row() -> dict[str, object]:
    fields = dict.fromkeys(
        ("authority", "confidence", "project", "status", "type", "valid_from", "valid_to",
         "language", "source_id", "byte_start", "byte_end")
    )
    return {**fields, "heading_ancestry": json.dumps(["The Vault Updates Its Own Code"]), "rank": -5.0,
            "source_path": "knowledge/notes/x.md", "title": "The Vault Updates Its Own Code",
            "content": CONTENT, "chunk_order": 0, "chunk_id": "c" * 64,
            "source_sha256": "a" * 64, "span_sha256": "b" * 64}


def _repeats_content(key: str, value: object) -> bool:
    if key in {"content", "title"} or not isinstance(value, str):
        return False
    return bool(value) and value in CONTENT


def test_no_field_of_a_generation_row_repeats_part_of_its_content() -> None:
    import generation_search
    import mcp_server

    row = mcp_server.agent_row(generation_search.generation_result(_row(), "g"))

    assert [key for key, value in row.items() if _repeats_content(key, value)] == []


def test_a_markdown_row_without_content_keeps_the_pages_own_summary(tmp_path, monkeypatch) -> None:
    import generation_search

    page = tmp_path / "knowledge" / "notes" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text(CONTENT, encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    read = generation_search.read_page(page, "page")

    hit = generation_search.page_hit(read, score=1.0, bm25_score=1.0)

    assert ("content" in hit, hit["summary"].startswith("the nightly pass")) == (False, True)
