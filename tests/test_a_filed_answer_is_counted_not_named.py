"""A filed answer's page is named in the tracked log only when it is published.

The slug is the question's own words, and `knowledge/log.md` is tracked. Research:
`docs/research/2026-09-14-a-filed-answer-is-counted-not-named.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import query_memory  # noqa: E402


def test_an_unpublished_filed_page_is_counted_and_a_published_one_named(tmp_path, monkeypatch):
    (tmp_path / ".gitignore").write_text("knowledge/notes/*\n!knowledge/notes/README.md\n", encoding="utf-8")
    monkeypatch.setattr(query_memory, "ROOT", tmp_path)
    notes = tmp_path / "knowledge" / "notes"

    phrases = [query_memory._filed_page_phrase(notes / name) for name in ("why-did-my-doctor-say-no.md", "README.md")]

    assert phrases == ["1 unpublished page", "`knowledge/notes/README.md`"]


def test_the_structure_guard_reads_a_back_quoted_page_path():
    from test_structure import _linked_note_paths

    line = "- 2026-09-14 — Filed Q&A `knowledge/notes/why-did-my-doctor-say-no.md` via `query_memory.py`."

    assert _linked_note_paths(line) == {"knowledge/notes/why-did-my-doctor-say-no.md"}
