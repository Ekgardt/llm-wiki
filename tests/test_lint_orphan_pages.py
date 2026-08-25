"""The orphan check must not demand what the index generator removes.

`rebuild_memory_index` drops superseded and archived pages from the navigation
map on purpose. While the orphan check still reported those pages as missing
from it, the two rules could not both hold, and `orphan_pages` is a blocking
category in CI — so the build had no green state available to it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

PAGE = """---
type: decision
title: "{title}"
status: {status}
---
# {title}

One-sentence summary: {title} exists.
"""


@pytest.fixture()
def notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A vault of its own; findings are reported relative to the vault root."""
    import lint_memory

    directory = tmp_path / "knowledge" / "notes"
    directory.mkdir(parents=True)
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    return directory


def _page(notes: Path, slug: str, status: str) -> Path:
    path = notes / f"{slug}.md"
    path.write_text(PAGE.format(title=slug, status=status), encoding="utf-8")
    return path


def _empty_index(tmp_path: Path) -> Path:
    index = tmp_path / "knowledge" / "index.md"
    index.write_text("# Session Memory Index\n", encoding="utf-8")
    return index


@pytest.mark.parametrize("status", ["superseded", "archived"])
def test_a_retired_page_is_not_an_orphan(
    tmp_path: Path, notes: Path, status: str
) -> None:
    import lint_memory

    page = _page(notes, "retired-page", status)

    assert lint_memory.check_orphans_against_index([page], _empty_index(tmp_path)) == []


def test_an_active_page_missing_from_the_index_is_still_an_orphan(
    tmp_path: Path, notes: Path
) -> None:
    """The check still does its job; only the retired pages are exempt."""
    import lint_memory

    page = _page(notes, "active-page", "active")

    findings = lint_memory.check_orphans_against_index(
        [page], _empty_index(tmp_path)
    )

    assert len(findings) == 1
    assert "active-page" in findings[0]


PUBLIC_GITIGNORE = """
knowledge/notes/*
!knowledge/notes/public-page.md
"""


def _publishing_vault(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(PUBLIC_GITIGNORE, encoding="utf-8")


def test_a_page_the_index_may_not_name_is_not_an_orphan(tmp_path: Path, notes: Path):
    """The index is tracked and names only published pages, by design.

    A compile writes private pages, so demanding their presence in the index
    demands a leak. The first successful compile of this vault raised exactly
    that finding on the page it had just written.
    """
    import lint_memory

    _publishing_vault(tmp_path)
    page = _page(notes, "private-page", "active")

    assert lint_memory.check_orphans_against_index([page], _empty_index(tmp_path)) == []


def test_a_published_page_missing_from_the_index_is_still_an_orphan(
    tmp_path: Path, notes: Path
):
    """The exemption is the publication boundary, not an amnesty."""
    import lint_memory

    _publishing_vault(tmp_path)
    page = _page(notes, "public-page", "active")

    findings = lint_memory.check_orphans_against_index([page], _empty_index(tmp_path))
    assert findings and "public-page" in findings[0]


def test_no_backlink_is_demanded_that_would_name_a_private_page(
    tmp_path: Path, notes: Path
):
    """The obligation sits on the target, and a published page cannot carry it."""
    import lint_memory

    _publishing_vault(tmp_path)
    private = _page(notes, "private-page", "active")
    public = _page(notes, "public-page", "active")
    private.write_text(
        private.read_text(encoding="utf-8")
        + "\nSee [[knowledge/notes/public-page]].\n",
        encoding="utf-8",
    )

    assert lint_memory.check_missing_backlinks([private, public], [notes]) == []
