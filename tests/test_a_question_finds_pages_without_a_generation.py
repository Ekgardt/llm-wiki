"""With no active generation a natural question still finds the pages that answer it.

See docs/research/2026-09-25-a-question-finds-pages-without-a-generation.md.
"""

from __future__ import annotations

from pathlib import Path

import search_memory


def _page(root: Path, name: str, body: str) -> Path:
    path = root / "knowledge" / "notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: concept\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return path


def test_a_question_in_russian_finds_the_page_that_shares_its_words(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    pages = [
        _page(tmp_path, "nightly-scheduler", "Мы выбрали systemd таймер вместо cron: он переживает перезагрузку."),
        _page(tmp_path, "unrelated", "Заметка о другом."),
    ]

    hits = search_memory._direct_markdown_hits(
        "почему systemd таймер, а не cron", pages, limit=5, project=None, since=None,
        as_of=None, deadline=None, cancelled=None,
    )

    assert [Path(hit["path"]).stem for hit in hits] == ["nightly-scheduler"]


def test_a_page_sharing_more_words_ranks_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    pages = [
        _page(tmp_path, "one-word", "cron only"),
        _page(tmp_path, "three-words", "systemd timer instead of cron"),
    ]

    hits = search_memory._direct_markdown_hits(
        "why a systemd timer and not cron", pages, limit=5, project=None, since=None,
        as_of=None, deadline=None, cancelled=None,
    )

    assert [Path(hit["path"]).stem for hit in hits] == ["three-words", "one-word"]
