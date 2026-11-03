"""A knowledge page that states no source_authority ranks as inferred (audit 2026-09-27 C-15).

docs/research/2026-09-27-an-unstated-authority-is-inferred.md
"""
from __future__ import annotations

from provenance import AUTHORITY_WEIGHTS


def test_the_markdown_fallback_ranks_an_unstated_page_as_inferred(tmp_path, monkeypatch) -> None:
    import search_memory

    page = tmp_path / "knowledge" / "notes" / "a-page.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\n---\n# A page\n\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    read = search_memory._read_page(page, "page")

    assert (read.authority, search_memory.trust_weight(read.authority, "concept", read.relative_path)) == (
        "", AUTHORITY_WEIGHTS["inferred"] * 1.15
    )
