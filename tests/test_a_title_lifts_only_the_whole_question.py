"""The Markdown fallback lifts a title only when it holds the whole question (audit 2026-09-26 C-10).

docs/research/2026-09-26-a-title-lifts-only-the-whole-question.md
"""
from __future__ import annotations

from pathlib import Path

import search_memory


def _page(root: Path, name: str, title: str, body: str) -> Path:
    path = root / "knowledge" / "notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: concept\n---\n# {title}\n\n{body}\n", encoding="utf-8")
    return path


def _ranked(query: str, pages: list[Path]) -> list[str]:
    hits = search_memory._direct_markdown_hits(
        query, pages, limit=5, project=None, since=None, as_of=None, deadline=None, cancelled=None
    )
    return [Path(hit["path"]).stem for hit in hits]


def test_a_page_holding_the_question_outranks_one_sharing_a_title_word(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    pages = [
        _page(tmp_path, "secret-notes", "Secret notes", "Unrelated text."),
        _page(tmp_path, "value-decides", "Value decides", "The redactor judges a secret by its shape."),
    ]

    assert _ranked("redactor secret shape", pages) == ["value-decides", "secret-notes"]


def test_a_title_that_is_the_whole_question_still_lifts_its_page(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    pages = [
        _page(tmp_path, "note-b", "Other", "secret and shape in the body."),
        _page(tmp_path, "note-a", "Secret shape", "Body."),
    ]

    assert _ranked("secret shape", pages)[0] == "note-a"
