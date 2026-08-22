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
