"""Wikilinks in *tracked* knowledge must resolve to *tracked* targets.

Catches the CI-only failure: link works locally via a gitignored personal
file (e.g. knowledge/projects/llm-wiki/re-setup.md) but fails on clean checkout.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import lint_memory
import pytest


def _tracked_knowledge_pages() -> list[Path]:
    raw = subprocess.check_output(
        ["git", "ls-files", "knowledge"],
        cwd=str(lint_memory.ROOT),
        text=True,
    )
    pages: list[Path] = []
    for line in raw.splitlines():
        if not line.endswith(".md"):
            continue
        if "/daily/" in line.replace("\\", "/"):
            continue
        p = lint_memory.ROOT / line
        if p.is_file():
            pages.append(p)
    return pages


def test_no_broken_wikilinks_in_tracked_knowledge():
    """Same bar as CI: only files that ship in git."""
    push_url = subprocess.check_output(
        ["git", "remote", "get-url", "--push", "origin"],
        cwd=str(lint_memory.ROOT),
        text=True,
    ).strip()
    if push_url == "no-push":
        pytest.skip("installed vault index intentionally references personal gitignored pages")
    pages = _tracked_knowledge_pages()
    assert pages, "expected tracked knowledge pages"
    broken = lint_memory.check_broken_links(
        pages, [lint_memory.MEMORY, lint_memory.WIKI]
    )
    assert broken == [], (
        "broken wikilinks (or links to untracked/gitignored files):\n"
        + "\n".join(broken)
    )


def test_untracked_target_is_reported(tmp_path, monkeypatch):
    """Regression: gitignored file must NOT satisfy a wikilink."""
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    page = notes / "alpha.md"
    page.write_text("# A\n\nSee [[knowledge/projects/demo/secret]].\n", encoding="utf-8")
    secret = vault / "knowledge" / "projects" / "demo" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("# secret\n", encoding="utf-8")

    monkeypatch.setattr(lint_memory, "ROOT", vault)
    monkeypatch.setattr(lint_memory, "MEMORY", vault / "knowledge")
    monkeypatch.setattr(lint_memory, "KNOWLEDGE", notes)
    monkeypatch.setattr(lint_memory, "WIKI", notes)
    monkeypatch.setattr(lint_memory, "DAILY_DIR", vault / "knowledge" / "daily")
    monkeypatch.setattr(
        lint_memory, "_git_tracked_paths", lambda: {"knowledge/notes/alpha.md"}
    )

    broken = lint_memory.check_broken_links([page], [vault / "knowledge", notes])
    assert broken, "expected untracked target to be reported as broken"
    assert any("untracked" in b for b in broken)


def test_untracked_source_does_not_require_backlink(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    tracked = notes / "tracked.md"
    tracked.write_text("# Tracked\n", encoding="utf-8")
    personal = notes / "personal.md"
    personal.write_text("# Personal\n\n[[tracked]]\n", encoding="utf-8")

    monkeypatch.setattr(lint_memory, "ROOT", vault)
    monkeypatch.setattr(
        lint_memory, "_git_tracked_paths", lambda: {"knowledge/notes/tracked.md"}
    )

    missing = lint_memory.check_missing_backlinks([tracked, personal], [notes])
    assert missing == []


def test_untracked_page_is_not_reported_as_orphan(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    index = vault / "knowledge" / "index.md"
    index.write_text("# Index\n", encoding="utf-8")
    personal = notes / "personal.md"
    personal.write_text("# Personal\n", encoding="utf-8")

    monkeypatch.setattr(lint_memory, "ROOT", vault)
    monkeypatch.setattr(lint_memory, "_git_tracked_paths", lambda: set())

    orphans = lint_memory.check_orphans_against_index([personal], index)
    assert orphans == []
