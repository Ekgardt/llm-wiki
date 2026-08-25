"""The OKF migrator must not write frontmatter the linter never asks for.

A weekly maintenance pass stamped OKF frontmatter onto `knowledge/notes/README.md`
and `knowledge/projects/README.md` — two tracked public files whose own first line
says directory READMEs are exempt. The writer and the checker disagreed: the
linter exempts every editorial name, the migrator only exempted the vault root.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from vault_editorial import EDITORIAL_NAMES  # noqa: E402


@pytest.fixture()
def notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import migrate_to_okf

    directory = tmp_path / "knowledge" / "notes"
    directory.mkdir(parents=True)
    monkeypatch.setattr(migrate_to_okf, "ROOT", tmp_path)
    return directory


@pytest.mark.parametrize("name", ["README.md", "state.md"])
def test_an_editorial_page_is_left_alone(notes: Path, name: str) -> None:
    import migrate_to_okf

    page = notes / name
    original = "# Directory readme\n\nDirectory READMEs are exempt.\n"
    page.write_text(original, encoding="utf-8")

    status, content = migrate_to_okf.migrate_file(page)

    assert status == "skip_editorial"
    assert content is None
    assert page.read_text(encoding="utf-8") == original


def test_every_editorial_name_is_skipped_wherever_it_sits(notes: Path) -> None:
    """The migrator and the linter read one list, so they cannot drift apart."""
    import migrate_to_okf

    for name in EDITORIAL_NAMES:
        page = notes / name
        page.write_text("# Page\n", encoding="utf-8")

        status, _content = migrate_to_okf.migrate_file(page)

        assert status in {"skip_editorial", "skip_reserved"}, name


def test_an_ordinary_note_still_gets_its_frontmatter(notes: Path) -> None:
    import migrate_to_okf

    page = notes / "ordinary-page.md"
    page.write_text("# Ordinary page\n\nBody.\n", encoding="utf-8")

    status, content = migrate_to_okf.migrate_file(page)

    assert status == "migrate"
    assert content is not None and content.startswith("---\ntype: concept\n")
