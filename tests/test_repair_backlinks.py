"""The vault owes its own backlinks; nobody should have to add them by hand."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_PAGE = "---\ntype: decision\n---\n# Target\n\nBody.\n"


def _source_page(root: Path, name: str) -> Path:
    return root / "knowledge" / "notes" / name


def test_the_link_lands_in_the_related_section_a_page_already_has():
    import repair_backlinks

    text = (
        "---\ntype: decision\n---\n# Target\n\nBody.\n\n"
        "## Related\n\n- [[knowledge/notes/other]]\n\n"
        "## Source / Evidence\n\n- somewhere\n"
    )
    result = repair_backlinks.with_backlink(text, "knowledge/notes/src")

    lines = result.split("\n")
    added = lines.index("- [[knowledge/notes/src]] — links to this page.")
    assert lines.index("- [[knowledge/notes/other]]") < added
    assert added < lines.index("## Source / Evidence")


def test_a_page_without_a_related_section_gets_one_and_nothing_else_moves():
    import repair_backlinks

    result = repair_backlinks.with_backlink(_PAGE, "knowledge/notes/src")

    assert result.startswith(_PAGE.rstrip("\n"))
    assert result.rstrip("\n").endswith(
        "## Related\n\n- [[knowledge/notes/src]] — links to this page."
    )


def test_a_link_that_is_already_there_is_not_added_twice():
    import repair_backlinks

    once = repair_backlinks.with_backlink(_PAGE, "knowledge/notes/src")
    twice = repair_backlinks.with_backlink(once, "knowledge/notes/src")

    assert twice == once


def test_the_owed_backlink_is_written_through_a_transaction(tmp_path, monkeypatch):
    import lint_memory
    import repair_backlinks

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    for module in (repair_backlinks, lint_memory):
        monkeypatch.setattr(module, "ROOT", vault, raising=False)
        monkeypatch.setattr(module, "VAULT", vault / "knowledge", raising=False)
        monkeypatch.setattr(module, "NOTES", notes, raising=False)
    monkeypatch.setattr(repair_backlinks, "VAULT", vault / "knowledge")
    monkeypatch.setattr(repair_backlinks, "NOTES", notes)
    source = _source_page(vault, "source.md")
    target = _source_page(vault, "target.md")
    source.write_text(
        "---\ntype: decision\n---\n# Source\n\nSee [[knowledge/notes/target]].\n",
        encoding="utf-8",
    )
    target.write_text(_PAGE, encoding="utf-8")

    planned = repair_backlinks.repair(apply=False)
    applied = repair_backlinks.repair(apply=True)

    assert planned == ["WOULD LINK: knowledge/notes/target.md → source"]
    assert applied == ["LINKED: knowledge/notes/target.md → source"]
    assert "[[knowledge/notes/source]]" in target.read_text(encoding="utf-8")
    assert repair_backlinks.repair(apply=False) == []
