"""An archived page was unreachable even when a question named it exactly.

`_collect_pages` drops anything marked `archived` or `superseded`, which is
right for a general question and wrong for one case: asking for the page by
name. Until 2026-09-06 such a question returned nothing, and there was no way to
learn from the system that the page existed at all.

Forgotten memories in *Drosophila* persist as silent traces that a reminder cue
recovers, and the same work shows a permissive cue reconstructs things that were
never there. So the cue here is the narrowest available — the normalised query
must equal the filename stem exactly — and what comes back is labelled.

See `docs/research/2026-09-06-forgotten-not-gone.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import search_memory  # noqa: E402


def _vault(tmp_path: Path, monkeypatch, **pages: str) -> Path:
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for name, body in pages.items():
        (notes / f"{name.replace('_', '-')}.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    return notes


ARCHIVED = "---\ntype: concept\nstatus: archived\n---\n# Old Idea\n\nIt was tried.\n"
ACTIVE = "---\ntype: concept\n---\n# Old Idea\n\nThe current one.\n"


def test_an_archived_page_is_still_absent_from_the_pages_search_walks(
    tmp_path: Path, monkeypatch
) -> None:
    """The exclusion itself is unchanged: history does not compete on topic."""
    notes = _vault(tmp_path, monkeypatch, old_idea=ARCHIVED)

    assert search_memory._collect_pages(knowledge_dir=notes, root=tmp_path) == []


def test_naming_an_archived_page_brings_it_back(tmp_path: Path, monkeypatch) -> None:
    _vault(tmp_path, monkeypatch, old_idea=ARCHIVED)

    hits = search_memory._with_exact_page(
        [], [], "old-idea", project=None, since=None, as_of=None
    )

    assert [hit["path"] for hit in hits] == ["knowledge/notes/old-idea.md"]


def test_what_comes_back_says_it_is_retired(tmp_path: Path, monkeypatch) -> None:
    """A reader must be able to see what they were given."""
    _vault(tmp_path, monkeypatch, old_idea=ARCHIVED)

    hits = search_memory._with_exact_page(
        [], [], "old-idea", project=None, since=None, as_of=None
    )

    assert hits[0]["retired"] is True


def test_a_different_name_recalls_nothing(tmp_path: Path, monkeypatch) -> None:
    """Never similarity, never a topic — the stem has to be the name."""
    _vault(tmp_path, monkeypatch, old_idea=ARCHIVED)

    hits = search_memory._with_exact_page(
        [], [], "old-ideas-in-general", project=None, since=None, as_of=None
    )

    assert hits == []


def test_an_active_page_of_that_name_wins(tmp_path: Path, monkeypatch) -> None:
    """Recall is for what is otherwise unreachable, not a second copy."""
    notes = _vault(tmp_path, monkeypatch, old_idea=ACTIVE)
    pages = search_memory._collect_pages(knowledge_dir=notes, root=tmp_path)

    hits = search_memory._with_exact_page(
        [], pages, "old-idea", project=None, since=None, as_of=None
    )

    assert len(hits) == 1
    assert "retired" not in hits[0]


def test_an_empty_query_recalls_nothing(tmp_path: Path, monkeypatch) -> None:
    _vault(tmp_path, monkeypatch, old_idea=ARCHIVED)

    assert search_memory._retired_page_named("") is None
